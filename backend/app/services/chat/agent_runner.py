"""
Agent FC Runner（C2）— Function Calling 编排核心

按 CLAUDE.md §9.2.2 模块隔离：
    - 本模块只做循环调度，不实现工具
    - 所有工具调用经 app.tools.registry.dispatch()（禁止直接 import 具体 Tool 类）

按 §9.7 自检 #1：禁止"模块 A 直接 import 模块 B 的具体类"——
    runner 不直接 import OrderTool/ProductTool/PolicyService；
    通过 ToolContext + dispatch() 抽象隔离。

主循环（≤ MAX_AGENT_TURNS）：
    messages = [system, *history, user]
    for turn in 1..MAX_AGENT_TURNS:
        resp = llm.chat(messages, tools=to_openai_tools(), tool_choice="auto")
        if resp["tool_calls"]:
            append assistant tool_calls msg
            for each tc:
                result = dispatch(name, args, ctx)
                append tool result msg
            continue    # 下一轮 LLM 综合
        else:
            # 最终答案：伪流式 yield（避免 2 次 LLM 调用）
            yield tokens
            yield done
            return

    # 超限：返 fallback 文案
    yield tokens + done

异常策略：
    - ENABLE_AGENT_FC=False → 抛 RuntimeError（orchestrator 顶层 fallback 处理）
    - LLM 调用异常 / dispatch 异常 → 抛 RuntimeError（orchestrator 顶层 fallback 处理）
    - generator 内不 try/except（避免 yield 在 finally 内的 PEP 380 坑）
"""
import json
import logging
from typing import Any, Generator, Optional, Tuple

from app.core.config import settings
from app.core.providers.llm import get_llm_provider
from app.services.prompt_loader import get_prompt_loader
from app.tools.registry import ToolContext, dispatch, to_openai_tools

logger = logging.getLogger(__name__)


# =============================================================
# 内部辅助
# =============================================================
def _load_system_prompt() -> str:
    """加载 FC agent system prompt（agent_fc.yaml）。

    失败时 fallback 到硬编码最小可用 prompt（避免 prompt_loader 故障阻断 Agent）。
    """
    try:
        return get_prompt_loader().load("agent_fc")
    except Exception as e:
        logger.warning(
            f"加载 agent_fc prompt 失败，使用硬编码 fallback: {e}",
            extra={"intent": "agent_fc"},
        )
        return (
            "你是一个专业的电商客服助手，可以使用 lookup_order / search_product / "
            "search_policy 三个工具。回答控制在 200 字以内。"
        )


def _format_history(history: Optional[list[dict]]) -> list[dict]:
    """history → OpenAI messages 列表（仅保留 user/assistant/system；丢弃 tool_calls）。

    Why：OpenAI FC 不支持历史里的 tool_calls（需要完整 tool_call_id 链路），
    C2 仅作为 FC 主入口的上下文参考，不强制要求历史完整性。
    """
    if not history:
        return []
    msgs = []
    for h in history:
        role = h.get("role")
        content = h.get("content", "")
        if role in ("user", "assistant", "system") and content:
            msgs.append({"role": role, "content": content})
    return msgs


def _count_tool_calls(messages: list[dict]) -> int:
    """统计本会话累计工具调用次数（meta 暴露用）。"""
    return sum(
        1 for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    )


def _truncate_for_meta(d: dict, max_len: int = 500) -> dict:
    """meta 事件中 tool_result 截断（防 log 爆炸）。"""
    try:
        s = json.dumps(d, ensure_ascii=False)
        if len(s) > max_len:
            return {"_truncated": True, "_preview": s[:max_len]}
        return d
    except Exception:
        return {"_unserializable": True}


# =============================================================
# 主入口
# =============================================================
def run_stream_agent(
    query: str,
    user_id: Optional[int] = None,
    history: Optional[list[dict]] = None,
) -> Generator[Tuple[str, Any], None, None]:
    """Agent Function Calling 主入口（orchestrator 在 ENABLE_AGENT_FC=True 时调）。

    Args:
        query: 用户问题（必填，非空字符串）
        user_id: 用户 ID（未登录传 None；查订单类工具会被 dispatch 拒绝）
        history: 多轮历史 [{"role", "content"}]（不含 tool_calls）

    Yields:
        ("meta", dict) - 元信息：
            - {turn, tool_call: {id, name, arguments}}
            - {turn, tool_result: {id, name, result}}
            - {turn, final: True, tool_used_count}
            - {turn, max_turns_reached: True}
        ("token", str)  - 文本片段（伪流式逐字 yield）
        ("done", dict)  - {"answer": str}  最终答案

    Raises:
        ValueError: query 为空
        RuntimeError: ENABLE_AGENT_FC=False（让 orchestrator fallback 到 V1.2）
        Exception: LLM 调用或 dispatch 异常（让 orchestrator fallback）
    """
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    query = query.strip()

    if not settings.ENABLE_AGENT_FC:
        raise RuntimeError(
            "Agent FC 未启用（ENABLE_AGENT_FC=False），"
            "orchestrator 应 fallback 到 V1.2 RAG"
        )

    ctx = ToolContext(user_id=user_id)
    llm = get_llm_provider()
    openai_tools = to_openai_tools()
    max_turns = settings.MAX_AGENT_TURNS

    messages: list[dict] = [{"role": "system", "content": _load_system_prompt()}]
    messages.extend(_format_history(history))
    messages.append({"role": "user", "content": query})

    for turn in range(1, max_turns + 1):
        logger.info(
            f"agent_runner turn={turn} query={query[:40]!r} user_id={user_id}",
            extra={"intent": "agent_fc", "turn": turn},
        )

        resp = llm.chat(
            messages=messages,
            tools=openai_tools,
            tool_choice="auto",
            temperature=0.7,
        )

        tool_calls = resp.get("tool_calls")

        if tool_calls:
            # LLM 决定调工具：先追加 assistant 消息（含 tool_calls 字段）
            messages.append({
                "role": "assistant",
                "content": resp.get("reply") or "",
                "tool_calls": tool_calls,
            })

            # 逐个 dispatch 并追加 tool 消息
            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                tool_name = fn.get("name", "")
                tool_args_raw = fn.get("arguments", "{}") or "{}"
                tool_id = tc.get("id", "")

                yield ("meta", {
                    "turn": turn,
                    "tool_call": {
                        "id": tool_id,
                        "name": tool_name,
                        "arguments": tool_args_raw,
                    },
                })

                result = dispatch(tool_name, tool_args_raw, ctx)

                yield ("meta", {
                    "turn": turn,
                    "tool_result": {
                        "id": tool_id,
                        "name": tool_name,
                        "result": _truncate_for_meta(result),
                    },
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            # 进入下一轮 LLM 综合
            continue

        # 无 tool_calls：LLM 已给出最终答复
        final_reply = resp.get("reply") or ""
        yield ("meta", {
            "turn": turn,
            "final": True,
            "tool_used_count": _count_tool_calls(messages),
        })

        # 伪流式 yield（避免 2 次 LLM 调用；UX 由前端 chunking 弥补）
        for ch in final_reply:
            yield ("token", ch)

        yield ("done", {"answer": final_reply})
        return

    # 达到 MAX_AGENT_TURNS 上限：返 fallback 文案（不走 LLM）
    logger.warning(
        f"agent_runner 达到最大工具轮次 {max_turns}: query={query[:40]!r}",
        extra={"intent": "agent_fc"},
    )
    fallback = "抱歉，处理这个问题时遇到了复杂度限制，请换个更具体的方式描述您的问题。"
    yield ("meta", {"turn": max_turns, "max_turns_reached": True})
    for ch in fallback:
        yield ("token", ch)
    yield ("done", {"answer": fallback})


__all__ = ["run_stream_agent"]