"""虚拟商品自动发货事件流（V21-P0.2）。

设计要点（区别于"购买后 if 发货"的硬编码分支）：

1. **事件驱动**：pay_order 只负责把订单置为 paid 并发布「订单已支付」事实，
   本 Flow 作为事件消费者接管虚拟商品的履约决策——实体商品无人消费该事件，
   仍走原有「模拟发货」人工/用户触发路径，两条路径互不感知。

2. **走 Agent 工具链而非裸调**：发货动作不直接操作库存表，而是复用
   AutoDeliveryTool 同一套审批门控（ApprovalGate）+ 交付服务（DeliveryService）：
   - 校验链：订单归属 / 已支付 / 商品类型 / 库存，全部 fail-fast
   - 审批票据：虚拟商品走「商家预授权」策略——批准者是平台策略而非人工坐席，
     但票据、幂等键绑定参数、审计记录一样不少（与 human-in-the-loop 共用同一机制）
   - 双层幂等：票据层 duplicate 拒绝 + 交付记录按单唯一，事件重复投递也不会重复发货

3. **可观测**：整条决策链落 agent_trace JSONL（channel=order_event，
   outcome=auto_delivery），运营指标（自动处理率）天然包含事件侧履约。

4. **非阻塞**：履约失败不影响付款主流程（付款已成功，发货可重试/人工兜底）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

TOOL_NAME = "auto_delivery"


class VirtualDeliveryFlow:
    """订单支付事件 → 虚拟商品自动履约（Agent 工具链）。"""

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_virtual_sku(db, order_no: str) -> Optional[str]:
        """订单行 → 商品 attributes.type == virtual 时返回 sku；否则 None。"""
        from sqlalchemy import select

        from app.models.order import Order, OrderItem
        from app.models.product import Product

        order = db.query(Order).filter(
            Order.order_no == order_no, Order.deleted == 0
        ).first()
        if order is None:
            return None
        items = db.query(OrderItem).filter(
            OrderItem.order_id == order.id, OrderItem.deleted == 0
        ).all()
        for it in items:
            product = db.scalar(select(Product).where(Product.sku == it.sku))
            if product is None:
                continue
            attrs = product.attributes
            if isinstance(attrs, str):
                try:
                    attrs = json.loads(attrs)
                except (TypeError, ValueError):
                    attrs = {}
            if isinstance(attrs, dict) and attrs.get("type") == "virtual":
                return it.sku
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def process_paid_order(user_id: int, order_no: str) -> Optional[dict[str, Any]]:
        """支付事件消费入口。虚拟商品返回履约时间线；实体商品返回 None。

        任何失败都不抛出（付款已成功，履约可重试/人工兜底），只记日志。
        """
        started = time.perf_counter()
        steps: list[dict[str, Any]] = []

        def _step(name: str, detail: str, ok: bool = True) -> None:
            steps.append({"name": name, "detail": detail, "ok": ok})

        try:
            # ---------- Step 1: 订单校验（归属 + 已支付） ----------
            from app.tools.order_tool import OrderTool

            order = OrderTool.get_order_by_no(user_id, order_no)
            if order is None:
                logger.warning("[virtual_delivery] 订单不存在或越权 order=%s", order_no)
                return None
            if order.get("status") != "paid":
                _step("订单校验", f"状态 {order.get('status')} 非已支付，跳过", ok=False)
                return VirtualDeliveryFlow._pack(order_no, steps, started, None)
            _step("订单校验", f"归属用户 {user_id} · 已支付 ¥{order.get('total_amount')}")

            # ---------- Step 2: 商品类型识别（事件订阅范围） ----------
            from app.clients.mysql_client import with_safe_session

            with with_safe_session() as db:
                sku = VirtualDeliveryFlow._resolve_virtual_sku(db, order_no)
            if sku is None:
                # 实体商品：不消费事件，走人工/用户触发发货路径
                return None
            _step("商品类型", f"{sku} · virtual(card) 订阅自动履约")

            # ---------- Step 3: 审批票据（商家预授权策略） ----------
            from app.services.agent_gating import get_approval_gate

            gate = get_approval_gate()
            payload = {"order_no": order_no, "sku": sku}
            ticket = gate.request(TOOL_NAME, user_id, payload)
            ok, reason = gate.verify_and_consume(TOOL_NAME, user_id, payload)
            if not ok:
                _step("审批票据", f"拒绝：{reason}", ok=False)
                logger.warning("[virtual_delivery] 票据校验失败 order=%s reason=%s", order_no, reason)
                return VirtualDeliveryFlow._pack(order_no, steps, started, None)
            _step("审批票据", f"商家预授权策略 · ticket={str(ticket.get('ticket_id'))[:12]}…")

            # ---------- Step 4: 卡密分配（双层幂等的交付层） ----------
            from app.services.delivery_service import get_delivery_service

            result = get_delivery_service().assign_card(
                order_no, user_id, sku, ticket_id=str(ticket.get("ticket_id", "")),
            )
            if result.get("error") or result.get("status") == "out_of_stock":
                _step("卡密分配", str(result.get("error") or "库存不足"), ok=False)
                logger.error("[virtual_delivery] 分配失败 order=%s result=%s", order_no, result)
                return VirtualDeliveryFlow._pack(order_no, steps, started, None)
            card = str(result.get("card_secret", ""))
            _step("卡密分配", f"库存扣减 · 交付记录 #{result.get('record_id', '-')}")

            # 时间线快照落库（订单列表/详情页展示决策链）
            get_delivery_service().save_timeline(order_no, steps, round((time.perf_counter() - started) * 1000, 1))

            # ---------- Step 5: 决策 trace（可观测） ----------
            VirtualDeliveryFlow._trace(user_id, order_no, sku, started)
        except Exception:  # noqa: BLE001 —— 履约异常不阻塞付款主流程
            logger.exception("[virtual_delivery] 履约异常 order=%s", order_no)
            _step("履约异常", "已记录日志，待重试或人工兜底", ok=False)
            return VirtualDeliveryFlow._pack(order_no, steps, started, None)

        return VirtualDeliveryFlow._pack(order_no, steps, started, card)

    # ------------------------------------------------------------------
    @staticmethod
    def _trace(user_id: int, order_no: str, sku: str, started: float) -> None:
        """落一条 agent_trace：事件侧履约计入自动处理率。"""
        try:
            from app.core.config import settings

            if not settings.AGENT_TRACE_ENABLED:
                return
            from app.services.chat.agent_trace import AgentTrace

            t = AgentTrace(f"evt:{order_no}", user_id, "order_event", f"auto_delivery {sku}")
            t.mark("intent_method", "event_auto_delivery")
            t.mark("cache_hit", True)  # 未产生 LLM 调用（确定性规则履约）
            t.mark("latency_ms", round((time.perf_counter() - started) * 1000, 1))
            t.flush("auto_delivery")
        except Exception:  # noqa: BLE001 —— 观测层不影响业务
            logger.debug("[virtual_delivery] trace 落盘失败（忽略）", exc_info=True)

    # ------------------------------------------------------------------
    @staticmethod
    def _pack(
        order_no: str, steps: list[dict[str, Any]], started: float, card: Optional[str]
    ) -> dict[str, Any]:
        latency = round((time.perf_counter() - started) * 1000, 1)
        out: dict[str, Any] = {
            "order_no": order_no,
            "steps": steps,
            "latency_ms": latency,
            "policy": "merchant_preauthorized",
        }
        if card:
            # 脱敏展示：前 5 位 + 尾 4 位
            out["card_masked"] = card if len(card) <= 9 else f"{card[:5]}****{card[-4:]}"
            out["card_secret"] = card  # 持有人（下单用户）可见完整卡密
            out["status"] = "delivered"
        else:
            out["status"] = "pending_manual"
        return out
