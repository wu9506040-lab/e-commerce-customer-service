"""
PhaseDetector — 订单 + 意图两阶段判定（V16.1.5b）

设计逻辑（用户拍板）：
1. 阶段1（订单硬条件）：查 OrderService.query_by_user_unb
   - 无订单 / 全 cancelled → PRESALE（售前）
   - pending_payment → PRESALE（未付款=售前）
   - shipped / delivering / paid → IN_SALE（售中）
   - completed / received → AFTER_SALE（售后）
2. 阶段2（意图软判定）：调 intent_service.classify
   - refund_query → AFTER_SALE
   - order_query → IN_SALE
   - policy_query / product_query / general → PRESALE
3. 综合：阶段1 优先（订单硬条件），但若阶段2 ≠ 阶段1 → 标 ambiguous=True（老客复购等场景）

调用方：
  ReplyScheduler → PhaseDetector.detect_phase(unb, last_message)
    → PhaseResult.phase + PhaseResult.ambiguous
    → 喂给 LLM（to_prompt_context）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class Phase(str, Enum):
    """客户阶段"""
    PRESALE = "presale"        # 售前
    IN_SALE = "in_sale"        # 售中
    AFTER_SALE = "after_sale"  # 售后

    @property
    def chinese(self) -> str:
        return {
            Phase.PRESALE: "售前",
            Phase.IN_SALE: "售中",
            Phase.AFTER_SALE: "售后",
        }[self]


class _OrderServiceProtocol(Protocol):
    """智能客服 OrderService 协议（duck typing）"""

    def query_by_user_unb(self, unb: str) -> list[dict]: ...


class _IntentClassifierProtocol(Protocol):
    """智能客服 intent_service 协议"""

    def classify(self, query: str, user_id: Optional[int] = None) -> dict: ...


# 订单状态 → 阶段映射
_ORDER_STATUS_TO_PHASE = {
    # 售前（无订单 / 未付款 / 已取消）
    "pending_payment": Phase.PRESALE,
    "cancelled": Phase.PRESALE,
    "closed": Phase.PRESALE,
    # 售中（已付款 / 配送中）
    "paid": Phase.IN_SALE,
    "shipped": Phase.IN_SALE,
    "delivering": Phase.IN_SALE,
    "to_be_shipped": Phase.IN_SALE,
    # 售后（已签收 / 已完成）
    "completed": Phase.AFTER_SALE,
    "received": Phase.AFTER_SALE,
    "refunding": Phase.AFTER_SALE,
    "refunded": Phase.AFTER_SALE,
}

# 意图 → 阶段映射
_INTENT_TO_PHASE = {
    "refund_query": Phase.AFTER_SALE,
    "order_query": Phase.IN_SALE,
    "policy_query": Phase.PRESALE,
    "product_query": Phase.PRESALE,
}


def _phase_by_orders(orders: list[dict]) -> Optional[Phase]:
    """从订单列表推阶段（取"最活跃"订单的状态）"""
    if not orders:
        return None
    # 优先级：售后 > 售中 > 售前
    # 即如果有任何售后订单就当售后，全是售中就是售中
    phases = []
    for o in orders:
        status = o.get("status", "").lower()
        phase = _ORDER_STATUS_TO_PHASE.get(status)
        if phase is not None:
            phases.append(phase)
    if not phases:
        return None
    # 优先级：AFTER_SALE > IN_SALE > PRESALE
    if Phase.AFTER_SALE in phases:
        return Phase.AFTER_SALE
    if Phase.IN_SALE in phases:
        return Phase.IN_SALE
    return Phase.PRESALE


def _phase_by_intent(intent_result: dict) -> Optional[Phase]:
    """从意图分类结果推阶段"""
    primary = intent_result.get("primary", "")
    return _INTENT_TO_PHASE.get(primary)


@dataclass
class PhaseResult:
    phase: Phase
    ambiguous: bool = False
    order_count: int = 0
    intent_method: str = ""
    order_error: str = ""  # 订单服务异常时填

    def to_prompt_context(self) -> str:
        lines = [f"【客户阶段】{self.phase.chinese}"]
        if self.ambiguous:
            lines.append("（注：订单状态与消息意图不一致，请结合上下文判断）")
        if self.order_count:
            lines.append(f"订单数：{self.order_count} 单")
        if self.intent_method:
            lines.append(f"意图判定方式：{self.intent_method}")
        if self.order_error:
            lines.append(f"订单查询异常：{self.order_error}")
        return "\n".join(lines)


class PhaseDetector:
    """两阶段 phase 检测

    复用智能客服已有：
    - OrderService.query_by_user_unb（订单硬条件）
    - intent_service.classify（意图软判定）

    V16.1.5b 设计：阶段1优先（订单硬条件），但 stage2 不一致时标 ambiguous
    """

    def __init__(
        self,
        order_service: _OrderServiceProtocol,
        intent_classifier: _IntentClassifierProtocol,
    ):
        self._order_service = order_service
        self._intent_classifier = intent_classifier

    def detect_phase(self, unb: str, last_message: str) -> PhaseResult:
        # 阶段 1: 订单
        order_phase: Optional[Phase] = None
        order_error = ""
        order_count = 0
        try:
            orders = self._order_service.query_by_user_unb(unb) or []
            order_count = len(orders)
            order_phase = _phase_by_orders(orders)
        except Exception as e:
            order_error = f"{type(e).__name__}: {str(e)[:100]}"
            logger.warning(
                f"PhaseDetector 订单查询失败 unb={unb}: {order_error}"
            )

        # 阶段 2: 意图
        intent_phase: Optional[Phase] = None
        intent_method = ""
        try:
            intent_result = self._intent_classifier.classify(
                last_message, user_id=None
            )
            intent_method = intent_result.get("method", "")
            intent_phase = _phase_by_intent(intent_result)
        except Exception as e:
            logger.warning(
                f"PhaseDetector 意图分类失败 unb={unb}: {type(e).__name__}: {e}"
            )

        # 综合
        if order_phase is not None and intent_phase is not None:
            ambiguous = order_phase != intent_phase
            return PhaseResult(
                phase=order_phase,  # 订单硬条件优先
                ambiguous=ambiguous,
                order_count=order_count,
                intent_method=intent_method,
                order_error=order_error,
            )
        if order_phase is not None:
            return PhaseResult(
                phase=order_phase,
                order_count=order_count,
                intent_method=intent_method,
                order_error=order_error,
            )
        if intent_phase is not None:
            return PhaseResult(
                phase=intent_phase,
                order_count=order_count,
                intent_method=intent_method,
                order_error=order_error,
            )
        # 都拿不到 → 默认售前
        return PhaseResult(
            phase=Phase.PRESALE,
            order_count=order_count,
            order_error=order_error,
        )


__all__ = ["Phase", "PhaseDetector", "PhaseResult"]