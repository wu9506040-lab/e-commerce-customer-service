"""多 Agent 协调器（V21 M3-2）：一句话双意图 → 双专家顺序协作。

解决的真实缺陷：买家一句话问两件事（"ORDxxx 我要退款 物流单帮我看看"），
V12 意图分类早已识别出 top-K=2 意图，但下游只答 primary——**第二个问题
从来没人答**。本模块把次意图交给对应专家补一轮完整回答。

为什么是顺序拼接而不是 asyncio.gather：
- 专家 dispatch 是同步生成器（内部 run_sync 桥接 ORM/LLM），真并行需要
  整个事件协议改造（交织 meta/token 流，前端配合改）。
- 顺序拼接 v1：次问题本来就没被回答，"晚几秒的完整回答"远好于"没有回答"。
  真并行列为后续改造项（依赖 stream 协议 v2）。

事件流契约：
  主专家流原样透传 → 次专家流前置一条 ("meta", {"handoff_secondary": ...})
  提示前端进入第二段回答 → 次专家流过滤 done/closed（收尾由外层统一）。

灰度：ENABLE_DUAL_INTENT_DISPATCH（默认 False），chat 入口按场景开启。
"""
from __future__ import annotations

import logging
from typing import Any, Generator, Optional, Tuple

from app.services.agents.base import AgentContext, AgentProtocol
from app.services.agents.registry import get_expert

logger = logging.getLogger(__name__)

_SKIP_EVENTS = {"done", "closed"}


def get_secondary_intent(intent_result: dict, primary: str) -> Optional[str]:
    """从 V12 多意图结果取次意图（与 primary 不同且已识别）。"""
    secondaries = get_secondary_intents(intent_result, primary)
    return secondaries[0] if secondaries else None


def get_secondary_intents(intent_result: dict, primary: str) -> list[str]:
    """V21 M3-4：取全部次意图（与 primary 不同、去重、保持原顺序）。

    顺序语义：
      - rule_multi（合并批逐条收集）：顺序 = 买家消息顺序 → 按用户提问顺序依次答；
      - LLM 多意图：顺序 = confidence 降序 → 先答最有把握的。
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in (intent_result or {}).get("intents") or []:
        it = (item or {}).get("intent")
        if it and it != primary and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def chain_with_secondary(
    primary_gen: Generator[Tuple[str, Any], None, None],
    ctx: AgentContext,
    primary: str,
) -> Generator[Tuple[str, Any], None, None]:
    """主专家流 + 全部次专家补充流的合成生成器（V21 M3-4 多意图依次答）。

    M3-2 只补答第一个次意图；M3-4 扩展为遍历全部次意图——对应连发合并批
    "在吗 + 多大 + 什么时候发货"识别出的多意图（intent_service 闲聊让路产出），
    每个次意图都由对应专家补一轮完整回答，按 intents 列表顺序串行。

    次专家失败（异常/未注册）只打日志——补充回答是增量价值，
    绝不能因为它破坏主回答（与 trace 同一哲学）。
    """
    for event in primary_gen:
        yield event

    # ---- 主流结束，尝试次意图补充 ----
    from app.core.config import settings

    if not getattr(settings, "ENABLE_DUAL_INTENT_DISPATCH", False):
        return

    secondaries = get_secondary_intents(ctx.intent_result, primary)
    if not secondaries:
        return

    for secondary_intent in secondaries:
        secondary_agent = get_expert(secondary_intent)
        if secondary_agent is None:
            continue

        logger.info(
            f"[dual-dispatch] primary={primary} done, 补答次意图 {secondary_intent} "
            f"(expert={secondary_agent.name})",
            extra={"intent": secondary_intent, "dual": True},
        )

        # 前置提示事件（前端可渲染"关于您的第二个问题"分隔）
        yield ("meta", {
            "intent": secondary_intent,
            "handoff_secondary": True,
            "secondary_expert": secondary_agent.name,
        })

        sec_ctx = AgentContext(
            query=ctx.query, user_id=ctx.user_id,
            intent_result={"primary": secondary_intent, "intents": [{"intent": secondary_intent}]},
            session_id=ctx.session_id, order_no=ctx.order_no, sku=ctx.sku,
            context_block=ctx.context_block, history=ctx.history,
            channel=ctx.channel, buyer_unb=ctx.buyer_unb,
        )
        try:
            for event in secondary_agent.dispatch(sec_ctx):
                if event[0] in _SKIP_EVENTS:
                    continue
                yield event
        except Exception as e:  # noqa: BLE001 —— 次回答失败不影响主回答
            logger.warning("[dual-dispatch] 次意图补充失败（忽略）: %s", e)
            yield ("token", "")
