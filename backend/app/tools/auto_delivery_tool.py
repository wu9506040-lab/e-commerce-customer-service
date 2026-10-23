"""虚拟商品自动发货 Tool（V21 P0 · 业务打通 + ApprovalGate 消费者）。

两步确认 + 审批票据（与 order_modify_tool 同一模式）：
- confirmed=False：校验订单（存在/归属/已支付/虚拟商品）→ 申请票据 →
  返 needs_confirmation（带 ticket 与订单校验结果）
- confirmed=True：verify_and_consume → DeliveryService.assign_card（幂等）
  → 返回卡密

校验链（全部 fail-fast，读操作不受门控影响）：
  1. 订单存在且属于该 user（OrderTool 复用，防越权）
  2. 订单已支付（status ∈ paid/shipped/delivered/completed）
  3. SKU 有库存（stock>0）
"""
from __future__ import annotations

import logging
from typing import Optional

from app.services.agent_gating import get_approval_gate
from app.services.delivery_service import get_delivery_service
from app.tools.order_tool import OrderTool

logger = logging.getLogger(__name__)

TOOL_NAME = "auto_delivery.deliver"
_PAID_STATUSES = {"paid", "shipped", "delivered", "completed"}


def deliver(
    user_id: int,
    order_no: str,
    sku: str,
    confirmed: bool = False,
) -> dict:
    """虚拟商品自动发货（两步确认 + 审批票据）。

    Returns:
        confirmed=False: {"status": "needs_confirmation", "ticket": {...}, ...}
        confirmed=True:  {"status": "delivered", "card_secret": ...}
        校验失败:        {"status": "order_not_found" | "order_not_paid" |
                          "not_owner"(隐含在 not_found) | "out_of_stock" | "write_gated"}
    """
    gate = get_approval_gate()
    payload = {"order_no": order_no, "sku": sku, "user_id": user_id}

    # ---- 校验链（confirmed 前都要做，早失败） ----
    order = OrderTool.get_order_by_no(user_id, order_no)
    if order is None:
        return {"status": "order_not_found",
                "hint": "订单不存在或不属于当前用户"}
    if order.get("status") not in _PAID_STATUSES:
        return {"status": "order_not_paid",
                "hint": f"订单状态 {order.get('status')}，未支付订单不能发货"}

    stock = get_delivery_service().stock(sku)
    if stock <= 0:
        return {"status": "out_of_stock", "hint": f"SKU {sku} 卡密库存不足"}

    # ---- Step 1: 未确认 → 申请票据，返 needs_confirmation ----
    if not confirmed:
        ticket = gate.request(TOOL_NAME, user_id, payload)
        return {
            "status": "needs_confirmation",
            "prompt": f"即将为订单 {order_no} 发放虚拟商品（SKU {sku}）卡密，请用户回复「确认」。",
            "requires_user_input": True,
            "ticket": ticket,
            "order": {"order_no": order_no, "status": order.get("status")},
        }

    # ---- Step 2: 已确认 → 校验并消费票据 → 发货 ----
    # request 幂等：同 payload 取回原 pending 票据（或旧票已执行后新建，
    # 此时 assign_card 的 order_no 唯一键会返回原卡密——双层幂等）
    ticket = gate.request(TOOL_NAME, user_id, payload)
    ok, reason = gate.verify_and_consume(TOOL_NAME, user_id, payload)
    if not ok:
        return {"status": "write_gated", "reason": reason,
                "hint": "审批票据无效或已消费，请重新走确认流程"}

    result = get_delivery_service().assign_card(
        order_no, user_id, sku, ticket_id=ticket.get("ticket_id", ""),
    )
    # 双层幂等：票据层（duplicate 拒绝）+ 交付层（order_no 唯一返回原卡密）
    logger.info(
        "[auto_delivery] order=%s sku=%s -> %s (duplicate=%s)",
        order_no, sku, result["status"], result.get("duplicate", False),
    )
    return result
