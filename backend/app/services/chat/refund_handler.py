"""
Refund Handler（Sprint 3 拆分自 chat/orchestrator.py）

职责：refund_query 意图的两条实现路径（V2 RefundService + V3 LangGraph）
- handle_refund_v2：USE_LANGGRAPH_REFUND=false 或 V3 异常时 fallback；调 RefundService.check_refundable_with_policy
- handle_refund_v3：默认路径；走 refund_graph_app.stream()，judge/fetch_policy/synthesize/escalate 节点顺序处理

边界：
- 不构造 prompt（委托 prompt_assembler）
- 不做 LLM 流式（委托 stream_dispatcher；V3 LangGraph 自身处理 token 输出）
- 不做 intent 分派（委托 orchestrator 调用本模块的函数）

Sprint 3 拆分原因：orchestrator.py 缩到 ≤ 350 行要求；refund 两条路径合 195 行，可下沉到本模块。
"""
import logging
from typing import Any, Generator, Optional, Tuple

from app.services.metrics import metrics  # noqa: F401  （保留以便后续加埋点）
from app.services.order_service import OrderService
from app.services.refund_graph import refund_graph_app  # V3 LangGraph 版
from app.services.refund_service import RefundService
from app.services.session_service import ANONYMOUS_USER_ID

from app.services.chat import prompt_assembler, stream_dispatcher

logger = logging.getLogger(__name__)


def handle_refund_v2(
    query: str, user_id: int, intent_result: dict,
    order_no: Optional[str] = None,
    context_block: str = "",
) -> Generator[Tuple[str, Any], None, None]:
    """refund_query V2.x：调 RefundService（复合 tool + policy）

    V3 起作为 fallback：USE_LANGGRAPH_REFUND=false 时使用，或 LangGraph 版异常时回退。

    .. deprecated::
        V3 LangGraph refund_graph 上线后的临时双轨态。V3 稳定后（预计下一个里程碑）
        删除本函数 + 关闭 USE_LANGGRAPH_REFUND 开关 + 删除对应测试。
        截止 2026-06-28：chat_e2e #5/#6 已用 V3 路径通过。
    """
    entities = intent_result["entities"]
    # M9.5：优先用 context 传来的 order_no（用户从订单卡片跳转退款）
    effective_order_no = order_no or entities.get("order_no")

    if user_id == ANONYMOUS_USER_ID:
        yield ("meta", {
            "intent": "refund_query",
            "entities": entities,
            "contexts": [],
            "scores": [],
        })
        yield from stream_dispatcher.stream_simple(prompt_assembler.NO_LOGIN_PROMPT)
        return

    # 无 order_no：取最近一笔订单的 order_no
    if not effective_order_no:
        recent = OrderService.list_user_orders(user_id)
        recent = recent[:1] if recent else []
        if recent:
            effective_order_no = recent[0]["order_no"]
        else:
            yield ("meta", {
                "intent": "refund_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
            })
            yield from stream_dispatcher.stream_simple("用户当前没有订单，无法判断退款。请提供订单号。")
            return

    result = RefundService.check_refundable_with_policy(user_id, effective_order_no, query)
    tool_block = prompt_assembler._format_tool_result("refund_query", result)
    policy_docs = result.get("policy_docs", [])
    policy_block = prompt_assembler._format_policy_docs(policy_docs)

    # P0-H：把退款判断 + 政策命中一并暴露给 meta
    contexts, scores = prompt_assembler._build_meta_contexts(policy_docs=policy_docs)
    meta = {
        "intent": "refund_query",
        "entities": entities,
        "contexts": contexts,
        "scores": scores,
        "order_no": effective_order_no,
        "refundable": result.get("tool_result", {}).get("refundable"),
        "policy_hits": len(policy_docs),
    }
    yield ("meta", meta)

    prompt = prompt_assembler._build_chat_prompt(
        intent="refund_query",
        tool_block=tool_block,
        policy_block=policy_block,
        product_block="",
        history_block="",
        query=query,
        context_block=context_block,
    )
    yield from stream_dispatcher.stream_llm(prompt)


def handle_refund_v3(
    query: str, user_id: int, intent_result: dict,
    order_no: Optional[str] = None,
    context_block: str = "",
    history: Optional[list[dict]] = None,
) -> Generator[Tuple[str, Any], None, None]:
    """refund_query V3：走 LangGraph refund_graph_app.stream()

    与 V2 区别：
    - LLM 调用在 LangGraph Node 6（synthesize_answer），不在 synthesizer
    - 支持「质量问题无凭证 → escalate」升级人工路径
    - LangGraph 异常 → fallback 到 handle_refund_v2

    SSE 协议兼容：
    - judge Node → yield meta（含 refundable / reason / days_since_order）
    - fetch_policy Node → 仅 log，不 yield meta
    - synthesize / escalate Node → yield token（final_answer 作为整体 token）
    - done 事件由 api/chat.py 统一处理（write-through）

    M9.5：context_block 透传给 LangGraph state，让 synthesize_answer 节点能看到订单 context
    M9.5+：history 透传给 LangGraph state，让 synthesize_answer / judge 能从历史提取 order_no
    """
    entities = intent_result["entities"]
    # M9.5：优先用 context 传来的 order_no（用户从订单卡片跳转退款）
    # M9.5+：其次用 intent 解析出的；最后从 history 中最近一条提到 ORD... 的消息兜底
    effective_order_no = (
        order_no
        or entities.get("order_no")
        or prompt_assembler._extract_order_no_from_history(history)
    )

    # 1. 鉴权（与 V2 一致）
    if user_id == ANONYMOUS_USER_ID:
        yield ("meta", {
            "intent": "refund_query",
            "entities": entities,
            "contexts": [],
            "scores": [],
            "v3_engine": "langgraph",
        })
        yield from stream_dispatcher.stream_simple(prompt_assembler.NO_LOGIN_PROMPT)
        return

    # 2. order_no 兜底（M9.5 修复：禁止自动 fallback 到最近订单，防止串单）
    # 根因：之前 fallback 会偷换成「最近订单」，导致 LLM 用错误订单的事实回答
    # 修复：无 order_no 时直接请用户提供，禁止推测
    if not effective_order_no:
        yield ("meta", {
            "intent": "refund_query",
            "entities": entities,
            "contexts": [],
            "scores": [],
            "v3_engine": "langgraph",
        })
        yield from stream_dispatcher.stream_simple(
            "请提供要查询退款的订单号（格式示例：ORD20260628004）。"
        )
        return

    # 3. 调 LangGraph refund_graph_app.stream() 边执行边输出
    meta_emitted = False
    try:
        for event in refund_graph_app.stream(
            {
                "user_id": user_id,
                "order_no": effective_order_no,
                "query": query,
                "context_block": context_block,  # M9.5：注入 context 让 synthesize 看得到
                "history": history or [],  # M9.5+：注入历史让 synthesize 能引用上下文
            },
            stream_mode="updates",  # 每步返回 {node_name: state_update}
        ):
            for node_name, state_update in event.items():
                # 跳过 __start__ / __end__ 哨兵节点
                if node_name.startswith("__"):
                    continue

                if node_name == "judge":
                    yield ("meta", {
                        "intent": "refund_query",
                        "entities": entities,
                        "contexts": [],
                        "scores": [],
                        "order_no": effective_order_no,
                        "v3_engine": "langgraph",
                        "refundable": state_update.get("refundable"),
                        "reason": state_update.get("reason"),
                        "days_since_order": state_update.get("days_since_order"),
                    })
                    meta_emitted = True
                elif node_name == "fetch_policy":
                    logger.info(
                        f"refund_v3 fetch_policy: order={effective_order_no} "
                        f"hits={len(state_update.get('policy_docs', []))}"
                    )
                elif node_name in ("synthesize", "escalate"):
                    if not meta_emitted:
                        # 兜底：理论上 judge 一定先于 synthesize
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "v3_engine": "langgraph",
                        })
                        meta_emitted = True
                    chunk = state_update.get("final_answer", "")
                    if chunk:
                        yield ("token", chunk)
        # 修复：refund_v3 主 LangGraph 流完成后补 yield done
        # 根因：_stream_llm/_stream_simple 末尾自动 yield done，但 LangGraph 路径不走它们
        # 影响：chat.py StopIteration → break → 缺 SSE done + write-through + latency 埋点
        yield ("done", {"answer": ""})
    except Exception as e:
        # LangGraph 挂了 → fallback 到 V2（保险丝）
        logger.exception(
            f"LangGraph refund 图执行失败，fallback 到 V2: order={effective_order_no} err={e}"
        )
        yield from handle_refund_v2(query, user_id, intent_result, order_no=order_no, context_block=context_block)
