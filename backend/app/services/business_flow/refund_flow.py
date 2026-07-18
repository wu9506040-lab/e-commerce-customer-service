"""RefundFlow - 退款业务流（M14 §10 阶段 3 + 2026-07-18 改造 + 2026-07-18 V3 转人工兜底）

包装 refund_graph V3（LangGraph 6 节点）：
  fetch_order → judge → fetch_policy → check_proof → escalate / synthesize

设计要点：
- 不替换 V3（D8 决策：Factory → RefundFlow → V3）
- yield meta.flow_stage 让前端展示阶段指示器（"正在审核 → 召回政策 → 生成回复"）
- LangGraph 异常 → fallback 到 V2（保留 handle_refund_v2 保险丝）
- V2 fallback 也失败 → 转人工兜底（M14 V3 新增，settings.ENABLE_ESCALATION_HANDOFF 灰度）
- yield 顺序与 handle_refund_v3 保持完全一致（向后兼容）
- 2026-07-18 改造：fetch_order 阶段接入 OrderContextResolver 自动解析
  - 真实业务场景：顾客不报订单号，CS 用系统查
  - 1 单 → DIRECT_ANSWER（自动用，无歧义）
  - N 单 → SHOW_PICKER（yield meta.card 让前端 OrderCard list 渲染）
  - 0 单 → ASK_LOGIN_OR_LIST
"""
from __future__ import annotations

import logging
from typing import Any, Generator, Optional, Tuple

from app.core.config import settings
from app.services.chat.refund_handler import handle_refund_v2
from app.services.chat.prompt_assembler import NO_LOGIN_PROMPT, _extract_order_no_from_history
from app.services.chat.stream_dispatcher import stream_simple
from app.services.context.context_service import ConversationContext
from app.services.context.order_context_resolver import (
    OrderResolverAction,
    get_order_context_resolver,
)
from app.services.escalation_service import (
    EscalationReason,
    get_escalation_service,
)
from app.services.refund_graph import refund_graph_app
from app.services.session_service import ANONYMOUS_USER_ID


def _build_card(result, density: str = "list", reason: str = "disambiguate") -> dict:
    """lazy import wrapper for orchestrator.Synthesizer._build_order_card_payload

    Why lazy: orchestrator → business_flow → refund_flow → orchestrator 形成环；
    refund_handler 已 lazy，但 RefundFlow 被 orchestrator._handle_refund 路径调，
    在业务模块启动期也会触发 chain。
    """
    from app.services.chat.orchestrator import Synthesizer
    return Synthesizer._build_order_card_payload(result, density=density, reason=reason)


def _yield_handoff(
    reason: EscalationReason,
    user_id: int,
    history: Optional[list[dict]],
    intent_result: Optional[dict],
    failure_context: Optional[dict] = None,
) -> Generator[Tuple[str, Any], None, None]:
    """yield 转人工事件（meta.handoff + token + done）

    灰度开关 ENABLE_ESCALATION_HANDOFF=False 时降级为"系统繁忙"文本（不暴露 payload）。
    复用入口：RefundFlow V2 fallback 失败 / handle_refund_v3 V2 fallback 失败。
    """
    if not settings.ENABLE_ESCALATION_HANDOFF:
        # 灰度关：降级为固定话术（不推 handoff payload）
        yield ("meta", {
            "intent": "refund_query",
            "entities": {},
            "contexts": [],
            "scores": [],
            "flow_stage": "escalate",
            "v3_engine": "escalation_disabled",
        })
        yield from stream_simple("系统繁忙，请稍后再试或联系人工客服。")
        yield ("done", {"answer": ""})
        return

    escalation = get_escalation_service()
    payload = escalation.handoff(
        reason=reason,
        user_id=user_id,
        history=history,
        intent_result=intent_result,
        failure_context=failure_context,
    )
    yield ("meta", {
        "intent": "refund_query",
        "entities": {},
        "contexts": [],
        "scores": [],
        "flow_stage": "escalate",
        "v3_engine": "escalation",
        "handoff": payload.to_dict(),
    })
    yield ("token", f"{payload.reason_label}（工单号 {payload.handoff_id}），人工客服会尽快联系您～")
    yield ("done", {"answer": ""})


logger = logging.getLogger(__name__)


class RefundFlow:
    """退款业务流：包装 LangGraph V3 + 显式 stage 推送

    阶段名（与 LangGraph node_name 对齐）：
    - fetch_order    → 查订单
    - judge          → 规则判断可退性
    - fetch_policy   → 召回政策条款
    - check_proof    → 检查用户凭证
    - escalate       → 升级人工
    - synthesize     → LLM 综合答案
    """

    name = "refund"

    def __init__(
        self,
        query: str,
        user_id: int,
        intent_result: dict,
        order_no: Optional[str] = None,
        context_block: str = "",
        history: Optional[list[dict]] = None,
    ) -> None:
        self.query = query
        self.user_id = user_id
        self.intent_result = intent_result
        self.order_no = order_no
        self.context_block = context_block
        self.history = history

    def run(self) -> Generator[Tuple[str, Any], None, None]:
        """执行 RefundFlow，按节点顺序 yield SSE 事件

        yield 内容：
        - ("meta", {...})：每个阶段都先 yield meta（含 flow_stage 字段）
        - ("token", str)：LLM 综合答案（synthesize 阶段）
        - ("done", {"answer": str})：流结束（langgraph 路径需手动 yield，refund_handler 同款）
        """
        entities = self.intent_result["entities"]
        # 与 handle_refund_v3 逻辑完全一致：context > entities > history 兜底
        effective_order_no = (
            self.order_no
            or entities.get("order_no")
            or _extract_order_no_from_history(self.history)
        )

        # 1. 鉴权：匿名用户 → 短路
        if self.user_id == ANONYMOUS_USER_ID:
            yield ("meta", {
                "intent": "refund_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
                "flow_stage": "fetch_order",
                "v3_engine": "langgraph",
            })
            yield from stream_simple(NO_LOGIN_PROMPT)
            return

        # 2. 无 order_no：走 Resolver 自动解析（真实业务场景：CS 用系统查顾客订单，而非问顾客）
        # 取代旧的"请提供订单号"prompt；M9.5 防串单通过 Resolver 0/1/N 决策兜底
        if not effective_order_no:
            resolver = get_order_context_resolver()
            ctx = ConversationContext(session_id="", user_id=self.user_id)
            result = resolver.resolve(self.user_id, "refund_query", entities, ctx)

            # 2.1 唯一 1 单 → 自动用（无歧义，安全）
            if result.action == OrderResolverAction.DIRECT_ANSWER and result.effective_order_no:
                effective_order_no = result.effective_order_no
                # 不 return，继续走 LangGraph

            # 2.2 N 单 → yield meta with card 让前端 OrderCard list 渲染
            elif result.action == OrderResolverAction.SHOW_PICKER:
                card = _build_card(result, density="list", reason="disambiguate")
                yield ("meta", {
                    "intent": "refund_query",
                    "entities": entities,
                    "contexts": [],
                    "scores": [],
                    "flow_stage": "fetch_order",
                    "v3_engine": "langgraph",
                    "card": card if settings.SSE_CARD_V2 else None,
                    "resolver_action": result.action.value,
                    "resolver_reason": result.reason,
                    "total_orders": result.total_orders,
                    "truncated": result.truncated,
                })
                text = (
                    f"您有 {result.total_orders} 个订单，请选择要退款的订单："
                    if not result.truncated
                    else f"您最近订单较多（{result.total_orders} 个），以下仅展示前 {len(card['items'])} 个："
                )
                yield from stream_simple(text)
                return  # 等待用户点选后下一轮带 order_no 进来

            # 2.3 0 单 → 明确告知
            elif result.action == OrderResolverAction.ASK_LOGIN_OR_LIST:
                yield ("meta", {
                    "intent": "refund_query",
                    "entities": entities,
                    "contexts": [],
                    "scores": [],
                    "flow_stage": "fetch_order",
                    "v3_engine": "langgraph",
                    "resolver_action": result.action.value,
                    "resolver_reason": result.reason,
                    "total_orders": 0,
                })
                yield from stream_simple("您当前没有订单，无法处理退款哦～")
                return

            # 2.4 NOT_FOUND（提供了无效 order_no）/ ASK_LOGIN（匿名）兜底
            elif result.action == OrderResolverAction.NOT_FOUND:
                yield ("meta", {
                    "intent": "refund_query",
                    "entities": entities,
                    "contexts": [],
                    "scores": [],
                    "flow_stage": "fetch_order",
                    "v3_engine": "langgraph",
                    "resolver_action": result.action.value,
                    "resolver_reason": result.reason,
                })
                yield from stream_simple("订单不存在或不属于当前用户，请检查。")
                return

            elif result.action == OrderResolverAction.ASK_LOGIN:
                yield ("meta", {
                    "intent": "refund_query",
                    "entities": entities,
                    "contexts": [],
                    "scores": [],
                    "flow_stage": "fetch_order",
                    "v3_engine": "langgraph",
                    "resolver_action": result.action.value,
                    "resolver_reason": result.reason,
                })
                yield from stream_simple(NO_LOGIN_PROMPT)
                return

        # 3. 调 LangGraph refund_graph_app.stream() 边执行边输出
        # 与 handle_refund_v3 的关键差异：每个节点都 yield meta + flow_stage
        # 前端可订阅 flow_stage 实现阶段指示器（如 "正在查订单 → 正在审核 → 召回政策 → 生成回复"）
        meta_emitted = False
        try:
            for event in refund_graph_app.stream(
                {
                    "user_id": self.user_id,
                    "order_no": effective_order_no,
                    "query": self.query,
                    "context_block": self.context_block,
                    "history": self.history or [],
                },
                stream_mode="updates",
            ):
                for node_name, state_update in event.items():
                    if node_name.startswith("__"):
                        continue

                    if node_name == "judge":
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": "judge",
                            "v3_engine": "langgraph",
                            "refundable": state_update.get("refundable"),
                            "reason": state_update.get("reason"),
                            "days_since_order": state_update.get("days_since_order"),
                        })
                        meta_emitted = True
                    elif node_name == "fetch_policy":
                        # 召回阶段：meta 推送（带 policy_hits），让前端能感知"正在查条款"
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": "fetch_policy",
                            "v3_engine": "langgraph",
                            "policy_hits": len(state_update.get("policy_docs", [])),
                        })
                    elif node_name == "check_proof":
                        # 凭证检查阶段：meta 推送（带 escalate_to_human）
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": "check_proof",
                            "v3_engine": "langgraph",
                            "escalate_to_human": state_update.get("escalate_to_human", False),
                        })
                    elif node_name in ("synthesize", "escalate"):
                        if not meta_emitted:
                            # 兜底：judge 一定先于 synthesize
                            yield ("meta", {
                                "intent": "refund_query",
                                "entities": entities,
                                "contexts": [],
                                "scores": [],
                                "order_no": effective_order_no,
                                "flow_stage": node_name,
                                "v3_engine": "langgraph",
                            })
                            meta_emitted = True
                        # 终止阶段：yield meta（带 stage）+ token（final_answer）
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": node_name,
                            "v3_engine": "langgraph",
                        })
                        chunk = state_update.get("final_answer", "")
                        if chunk:
                            yield ("token", chunk)

            # 修复：langgraph 路径需手动 yield done（与 handle_refund_v3 一致）
            yield ("done", {"answer": ""})

        except Exception as e:
            # LangGraph 挂了 → fallback 到 V2（保险丝）
            logger.exception(
                f"RefundFlow LangGraph 执行失败，fallback 到 V2: "
                f"order={effective_order_no} err={e}"
            )
            try:
                yield from handle_refund_v2(
                    self.query,
                    self.user_id,
                    self.intent_result,
                    order_no=self.order_no,
                    context_block=self.context_block,
                )
            except Exception as v2_err:
                # V2 fallback 也挂了 → 转人工兜底（M14 V3 新增）
                logger.exception(
                    f"RefundFlow V2 fallback 也失败，触发转人工: "
                    f"order={effective_order_no} v3_err={e} v2_err={v2_err}"
                )
                yield from _yield_handoff(
                    reason=EscalationReason.AGENT_UNAVAILABLE,
                    user_id=self.user_id,
                    history=self.history,
                    intent_result=self.intent_result,
                    failure_context={
                        "failed_stage": "v3_v2_both_failed",
                        "v3_error_class": type(e).__name__,
                        "v3_error_msg": str(e)[:200],
                        "v2_error_class": type(v2_err).__name__,
                        "v2_error_msg": str(v2_err)[:200],
                        "retry_count": 1,
                    },
                )
                return