"""四个意图域专家 Agent（V21 M3-1）。

包装现有 handler（**行为等价重构**，不是重写）：
- PolicyAgent  → Synthesizer._handle_policy
- ProductAgent → Synthesizer._handle_product
- OrderAgent   → Synthesizer._handle_order
- RefundAgent  → xianyu 分支 / BusinessFlow / LangGraph V3 / V2 四路（原 orchestrator 分支逻辑原样迁入）

每个专家保留自己的 metrics.inc_chat 计数（行为等价的关键：M8 看板口径不变）。
"""
from __future__ import annotations

import logging
from typing import Generator

from app.core.config import settings
from app.services.metrics import metrics
from app.services.agents.base import AgentContext, AgentProtocol

logger = logging.getLogger(__name__)


class PolicyAgent(AgentProtocol):
    name = "policy_expert"
    intents = ("policy_query",)

    def dispatch(self, ctx: AgentContext) -> Generator:
        from app.services.chat.orchestrator import Synthesizer

        metrics.inc_chat(ctx.intent_result.get("primary", "policy_query"), v3_engine="-")
        return Synthesizer._handle_policy(
            ctx.query, ctx.intent_result, ctx.history,
            context_block=ctx.context_block,
            search_queries=ctx.search_queries,
            secondary_intent_block=ctx.secondary_intent_block,
        )


class ProductAgent(AgentProtocol):
    name = "product_expert"
    intents = ("product_query",)

    def dispatch(self, ctx: AgentContext) -> Generator:
        from app.services.chat.orchestrator import Synthesizer

        metrics.inc_chat(ctx.intent_result.get("primary", "product_query"), v3_engine="-")
        return Synthesizer._handle_product(
            ctx.query, ctx.intent_result, ctx.history,
            sku=ctx.sku, context_block=ctx.context_block,
            search_queries=ctx.search_queries,
            secondary_intent_block=ctx.secondary_intent_block,
        )


class OrderAgent(AgentProtocol):
    name = "order_expert"
    intents = ("order_query",)

    def dispatch(self, ctx: AgentContext) -> Generator:
        from app.services.chat.orchestrator import Synthesizer

        metrics.inc_chat(ctx.intent_result.get("primary", "order_query"), v3_engine="-")
        return Synthesizer._handle_order(
            ctx.query, ctx.user_id, ctx.intent_result,
            order_no=ctx.order_no, context_block=ctx.context_block,
            session_id=ctx.session_id,
            secondary_intent_block=ctx.secondary_intent_block,
        )


class RefundAgent(AgentProtocol):
    """退款专家：四级降级阶梯（V21 收口——每级有明确失败条件与接管方）。

    xianyu bridge → ① RefundFlow（LangGraph 主引擎，ENABLE_BUSINESS_FLOW）
    → ② handle_refund_v2（图引擎异常保险丝，不依赖 LangGraph/Checkpointer）
    → ③ 转人工（EscalationService AGENT_UNAVAILABLE）。
    契约全文见 refund_handler.py 模块 docstring；V2 退役条件也写在那里
    （v2 兜底连续一个迭代周期零命中才可下线，防止永久双轨）。
    """

    name = "refund_expert"
    intents = ("refund_query",)

    def dispatch(self, ctx: AgentContext) -> Generator:
        from app.services.chat.refund_handler import handle_refund_v2, handle_refund_v3

        primary = ctx.intent_result.get("primary", "refund_query")

        if ctx.channel == "xianyu":
            # V16.8 P0-A：闲鱼买家走专用 handler（不触发商城 ASK_LOGIN 拦截）
            from app.services.xianyu.orchestrator_bridge import handle_xianyu_refund

            logger.info(
                "refund_query → xianyu (P0-A channel branch)",
                extra={"intent": primary, "channel": "xianyu"},
            )
            metrics.inc_chat(primary, v3_engine="xianyu")
            return handle_xianyu_refund(
                query=ctx.query,
                buyer_unb=ctx.buyer_unb or "",
                intent_result=ctx.intent_result,
            )

        flow = self._create_flow(ctx)
        if flow is not None:
            logger.info(f"refund_query → {flow.name}", extra={"intent": primary, "flow": flow.name})
            metrics.inc_chat(primary, v3_engine="flow")
            return flow.run()
        if settings.USE_LANGGRAPH_REFUND:
            logger.info("refund_query → LangGraph V3", extra={"intent": primary})
            metrics.inc_chat(primary, v3_engine="v3")
            return handle_refund_v3(
                ctx.query, ctx.user_id, ctx.intent_result,
                order_no=ctx.order_no, context_block=ctx.context_block,
                history=ctx.history,
                secondary_intent_block=ctx.secondary_intent_block,
                session_id=ctx.session_id,
            )
        metrics.inc_chat(primary, v3_engine="v2")
        return handle_refund_v2(
            ctx.query, ctx.user_id, ctx.intent_result,
            order_no=ctx.order_no, context_block=ctx.context_block,
            secondary_intent_block=ctx.secondary_intent_block,
        )

    @staticmethod
    def _create_flow(ctx: AgentContext):
        from app.services.business_flow import create_business_flow

        return create_business_flow(
            intent="refund_query",
            query=ctx.query,
            user_id=ctx.user_id,
            intent_result=ctx.intent_result,
            order_no=ctx.order_no,
            context_block=ctx.context_block,
            history=ctx.history,
        )
