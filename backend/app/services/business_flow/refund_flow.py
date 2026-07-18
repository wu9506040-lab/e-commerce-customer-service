"""RefundFlow - 退款业务流（M14 §10 阶段 3 + 2026-07-18 改造 + 2026-07-18 V3 转人工兜底 + 2026-07-19 V3 重构）

包装 refund_graph V3（LangGraph 4 节点）：
  decide → fetch_policy / synthesize / escalate

真实工作流重构（M14 V3 · 2026-07-19）：
- 客服进入会话时后台已查好 user_id + 客户最近订单 + 历史对话
- Agent 是"看着已有信息决策"，不是"现场调用工具查"
- judge_basic_refundable 移到 RefundFlow.run()（不在 LangGraph 节点里）
  - 因为"7 天时效判定"是纯业务规则，不需要 LLM，也不需要在状态机里绕一圈
  - 算出 refundable / reason / status_zh / days_since_order 直接注入 initial_state
- OrderContextResolver 注入 status_zh 到 candidate_orders（前端 / prompt 共用）
- decide_node 看 orders / refundable / reason / image_urls 决策
- 4 节点：decide（主决策） → fetch_policy（条件）→ synthesize / escalate（终点）
- escalate 节点 → EscalationService.handoff() + yield meta.handoff

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

import datetime
import logging
from typing import Any, Generator, Optional, Tuple

from app.core.config import settings
from app.services.chat.prompt_assembler import NO_LOGIN_PROMPT, _extract_order_no_from_history
from app.services.chat.stream_dispatcher import stream_simple
from app.services.context.context_service import ConversationContext
from app.services.context.order_context_resolver import (
    OrderResolverAction,
    STATUS_ZH_MAP,
    get_order_context_resolver,
)
from app.services.escalation_service import (
    EscalationReason,
    get_escalation_service,
)
from app.services.refund_graph import refund_graph_app
from app.services.session_service import ANONYMOUS_USER_ID
from app.tools.order_tool import OrderTool  # V3: judge 已移到 run()，OrderTool.get_order_by_no 在此调用
# 顶层 import handle_refund_v2 作 V2 fallback 保险丝。
# 循环导入方向：refund_handler.handle_refund_v3 用 lazy import 拿 RefundFlow（见 refund_handler.py），
# 故此处可安全顶层 import handle_refund_v2（refund_handler 顶层不 import refund_flow）。
from app.services.chat.refund_handler import handle_refund_v2


# =============================================================
# 业务规则（启动期加载一次）
# =============================================================
from app.services.config_loader import get_config_loader
_REFUND_RULES = get_config_loader().load("refund")
REFUND_WINDOW_DAYS: int = int(_REFUND_RULES.get("REFUND_WINDOW_DAYS", 7))
DELIVERY_OFFSET_DAYS: int = int(_REFUND_RULES.get("DELIVERY_OFFSET_DAYS", 2))


# =============================================================
# judge（移到 run()，不在 LangGraph 节点里）
# =============================================================
def _judge_basic_refundable(order: dict) -> Tuple[bool, str, int, str]:
    """基础规则判断（纯业务逻辑，不调 LLM）。

    Args:
        order: OrderTool.get_order_by_no 返回的订单 dict（含 status_zh）

    Returns:
        (refundable, reason, days_since_order, status_zh)
    """
    if not order:
        return False, "订单不存在", 0, ""

    create_time_str = order.get("create_time")
    if create_time_str:
        try:
            create_time = datetime.datetime.fromisoformat(create_time_str)
        except (ValueError, TypeError):
            create_time = datetime.datetime.now()
    else:
        create_time = datetime.datetime.now()

    status = order.get("status", "")
    if status in ("delivered", "completed"):
        delivery_time = create_time + datetime.timedelta(days=DELIVERY_OFFSET_DAYS)
        days = max(0, (datetime.datetime.now() - delivery_time).days)
    else:
        days = max(0, (datetime.datetime.now() - create_time).days)

    status_zh = STATUS_ZH_MAP.get(status, status)

    if status == "refunded":
        return False, "该订单已退款，无法重复申请", days, status_zh
    if status == "delivered":
        if days <= REFUND_WINDOW_DAYS:
            return True, f"已签收 {days} 天，在 {REFUND_WINDOW_DAYS} 天无理由退货期限内", days, status_zh
        return False, f"已签收 {days} 天，超过 {REFUND_WINDOW_DAYS} 天无理由退货期限", days, status_zh
    # pending / paid / shipped / completed：都可发起退款申请
    return True, f"订单状态「{status_zh}」，可发起退款申请", days, status_zh


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

        V3 重构（2026-07-19）：
        - judge_basic_refundable 移到 run()，不在 LangGraph 节点里
        - initial_state 注入：orders / order_info / refundable / reason / status_zh / days_since_order
          / resolver_result / decide_retry_count / dialog_turn_count / image_urls
        - decide_node 看这些字段做决策（不再现场查订单/算时效）
        - escalate 节点 → EscalationService.handoff() + yield meta.handoff SSE
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
        resolver_result_dict: Optional[dict] = None  # 注入 initial_state 用
        if not effective_order_no:
            resolver = get_order_context_resolver()
            ctx = ConversationContext(session_id="", user_id=self.user_id)
            result = resolver.resolve(self.user_id, "refund_query", entities, ctx)
            resolver_result_dict = result.to_dict()

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

        # 3. judge（移到 run()，不在 LangGraph 里）+ 准备 initial_state
        # 真实业务：CS 后台已查好订单，调 judge 算 refundable/reason，注入 initial_state
        order_info: dict = {}
        refundable = False
        reason = ""
        days_since_order = 0
        status_zh = ""
        orders_list: list = []

        if effective_order_no:
            try:
                order_info = OrderTool.get_order_by_no(self.user_id, effective_order_no) or {}
            except Exception as e:
                logger.warning(f"RefundFlow: OrderTool.get_order_by_no failed: {e}")
                order_info = {}

            if order_info:
                # 注入 status_zh（与 Resolver 一致）
                order_info["status_zh"] = STATUS_ZH_MAP.get(
                    order_info.get("status", ""), order_info.get("status", "")
                )
                refundable, reason, days_since_order, status_zh = _judge_basic_refundable(order_info)
                orders_list = [order_info]

        # 4. 拼 initial_state（V3 重构：注入 orders + resolver_result + image_urls 等）
        # image_urls 暂从 intent_result 拿（M14 当前 intent 不抽图片，留接口）
        image_urls = entities.get("image_urls") or []
        # dialog_turn_count 从 history 长度估算（每条消息算 0.5 轮，向上取整）
        dialog_turn_count = max(1, (len(self.history or []) + 1) // 2)

        initial_state: dict[str, Any] = {
            "user_id": self.user_id,
            "order_no": effective_order_no,
            "query": self.query,
            "context_block": self.context_block,
            "history": self.history or [],
            "orders": orders_list,
            "order_info": order_info,
            "refundable": refundable,
            "reason": reason,
            "status_zh": status_zh,
            "days_since_order": days_since_order,
            "resolver_result": resolver_result_dict or {},
            "decide_retry_count": 0,
            "dialog_turn_count": dialog_turn_count,
            "image_urls": image_urls,
        }

        # 5. 调 LangGraph refund_graph_app.stream() 边执行边输出
        # V3 重构后节点：decide / fetch_policy / synthesize / escalate（4 节点）
        # 前端可订阅 flow_stage 实现阶段指示器（"正在决策 → 召回政策 → 生成回复"）
        decide_result_emitted = False
        try:
            for event in refund_graph_app.stream(initial_state, stream_mode="updates"):
                for node_name, state_update in event.items():
                    if node_name.startswith("__"):
                        continue

                    if node_name == "decide":
                        # 决策阶段：meta 推送（带 decide_result 关键字段）
                        dr = state_update.get("decide_result") or {}
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": "decide",
                            "v3_engine": "langgraph",
                            "decision": dr.get("decision"),
                            "confidence": dr.get("confidence"),
                            "refundable": refundable,
                            "reason": reason,
                            "status_zh": status_zh,
                            "days_since_order": days_since_order,
                        })
                        decide_result_emitted = True
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
                    elif node_name == "synthesize":
                        if not decide_result_emitted:
                            yield ("meta", {
                                "intent": "refund_query",
                                "entities": entities,
                                "contexts": [],
                                "scores": [],
                                "order_no": effective_order_no,
                                "flow_stage": "decide",
                                "v3_engine": "langgraph",
                            })
                            decide_result_emitted = True
                        # 终止阶段：yield meta（带 stage）+ token（final_answer）
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": "synthesize",
                            "v3_engine": "langgraph",
                        })
                        chunk = state_update.get("final_answer", "")
                        if chunk:
                            yield ("token", chunk)
                    elif node_name == "escalate":
                        # M14 V3: escalate 节点产出 escalate_result → EscalationService.handoff()
                        if not decide_result_emitted:
                            yield ("meta", {
                                "intent": "refund_query",
                                "entities": entities,
                                "contexts": [],
                                "scores": [],
                                "order_no": effective_order_no,
                                "flow_stage": "decide",
                                "v3_engine": "langgraph",
                            })
                            decide_result_emitted = True
                        er = state_update.get("escalate_result") or {}
                        yield from _yield_handoff_from_decide(
                            priority=er.get("priority", "P2"),
                            category=er.get("category", "复杂场景"),
                            handoff_summary=er.get("handoff_summary", "需要人工协助"),
                            user_id=self.user_id,
                            history=self.history,
                            intent_result=self.intent_result,
                            decide_result=initial_state.get("decide_result", {}),
                        )
                        return  # escalate 后流结束

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


def _yield_handoff_from_decide(
    priority: str,
    category: str,
    handoff_summary: str,
    user_id: int,
    history: Optional[list[dict]],
    intent_result: Optional[dict],
    decide_result: Optional[dict],
) -> Generator[Tuple[str, Any], None, None]:
    """从 decide 节点 escalate 路径产出 HandoffPayload（与 _yield_handoff 类似但带 priority/category）。

    区别于 _yield_handoff（LangGraph 异常兜底）：
    - _yield_handoff 是 V3+V2 都挂了时触发 → reason=AGENT_UNAVAILABLE，无 priority
    - _yield_handoff_from_decide 是 LangGraph decide 决策 escalate → 业务规则触发
      → reason=BUSINESS_RULE，带 priority/category/matched_keyword/detected_category
    """
    if not settings.ENABLE_ESCALATION_HANDOFF:
        yield ("meta", {
            "intent": "refund_query",
            "entities": {},
            "contexts": [],
            "scores": [],
            "flow_stage": "escalate",
            "v3_engine": "escalation_disabled",
        })
        yield from stream_simple("您的情况需要人工客服协助，已为您转接，请稍候...")
        yield ("done", {"answer": ""})
        return

    escalation = get_escalation_service()
    payload = escalation.handoff(
        reason=EscalationReason.BUSINESS_RULE,
        user_id=user_id,
        history=history,
        intent_result=intent_result,
        failure_context=None,
        priority=priority,
        category=category,
        matched_keyword=handoff_summary,  # 把决定原因的关键词也带过去
        detected_category=category,  # 这里 category 是中文 label，detected_category 给原始 key
    )
    yield ("meta", {
        "intent": "refund_query",
        "entities": {},
        "contexts": [],
        "scores": [],
        "flow_stage": "escalate",
        "v3_engine": "langgraph",
        "handoff": payload.to_dict(),
    })
    yield ("token", f"{payload.reason_label}（工单号 {payload.handoff_id}），人工客服会尽快联系您～")
    yield ("done", {"answer": ""})