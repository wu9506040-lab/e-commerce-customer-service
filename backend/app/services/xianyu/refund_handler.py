"""
XianyuRefundHandler V16.3 — 退款场景智能处理（基于 DB）

设计要点（V16.3）：
- 智能客服**不操作退款**（只读不写）
- 查 xianyu_orders 表判断状态
- 已退款 → 直接拒绝（带证据：订单号 + 退款时间）
- 未退款 → 转人工（按状态定优先级）
- 无记录 → 转人工（安全）

调用方：
  ReplyScheduler.on_fire → 检测到退款意图
    → XianyuRefundHandler.handle_refund_request(seller_unb, unb, buyer_nick)
    → RefundDecision（should_reject / should_escalate / reply_text / order_state）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.services.xianyu.order_repository import XianyuOrderRepository

logger = logging.getLogger(__name__)


@dataclass
class RefundDecision:
    """退款场景决策结果"""

    should_reject: bool = False
    reject_reason: str = ""
    should_escalate: bool = False
    escalation_priority: str = "normal"  # normal / urgent
    reply_text: str = ""
    order_state: str = "unknown"
    order_no: Optional[str] = None
    order_updated_at: Optional[str] = None


# 状态 → escalation_priority 映射
_URGENT_STATUSES = {"signed", "received"}


class XianyuRefundHandler:
    """退款场景智能处理（V16.3）"""

    def __init__(self, db: Session, seller_unb: str):
        self._db = db
        self._seller_unb = seller_unb

    def handle_refund_request(
        self, unb: str, buyer_nick: str = ""
    ) -> RefundDecision:
        """处理买家退款请求

        流程：
        1. 查 DB 该买家最近一笔已退款订单 → 是 → 直接拒绝
        2. 查 DB 该买家最近一笔非退款订单 → 是 → 转人工
        3. 无任何订单记录 → 转人工（安全）
        """
        nick = buyer_nick or "您"

        # 1. 已退款订单？→ 直接拒绝
        refunded = XianyuOrderRepository.get_latest_refunded(
            self._db, self._seller_unb, unb
        )
        if refunded is not None:
            return RefundDecision(
                should_reject=True,
                reject_reason="已退款",
                should_escalate=False,
                reply_text=(
                    f"{nick} 您的订单 {refunded.order_no} "
                    f"已于 {refunded.updated_at:%Y-%m-%d %H:%M} 退款成功，"
                    f"请查银行流水（1-3 工作日到账）。"
                    f"如未到账请联系闲鱼官方客服 9510202，"
                    f"或在闲鱼 App 订单页查询详情。"
                ),
                order_state="refunded",
                order_no=refunded.order_no,
                order_updated_at=refunded.updated_at.isoformat() if refunded.updated_at else None,
            )

        # 2. 有未退款的订单？→ 转人工
        active = XianyuOrderRepository.get_active_order(
            self._db, self._seller_unb, unb
        )
        if active is not None:
            priority = "urgent" if active.status in _URGENT_STATUSES else "normal"
            status_zh = {
                "paid": "已付款待发货",
                "shipped": "已发货未签收",
                "signed": "已签收",
                "received": "已收货",
            }.get(active.status, active.status)
            return RefundDecision(
                should_reject=False,
                should_escalate=True,
                escalation_priority=priority,
                reply_text=(
                    f"{nick} 您的退款需求已登记，"
                    f"订单 {active.order_no} 状态：{status_zh}。"
                    f"专员 24h 内联系您处理。"
                ),
                order_state=active.status,
                order_no=active.order_no,
                order_updated_at=active.updated_at.isoformat() if active.updated_at else None,
            )

        # 3. 无订单记录 → 转人工（安全）
        return RefundDecision(
            should_reject=False,
            should_escalate=True,
            escalation_priority="normal",
            reply_text=(
                f"{nick} 您的退款需求已登记，专员 24h 内联系您处理。"
            ),
            order_state="unknown",
        )


__all__ = ["XianyuRefundHandler", "RefundDecision"]