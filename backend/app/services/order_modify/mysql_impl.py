"""
MySQL 默认实现 — MySQLOrderModifyService（Sprint 18-C）

实现 services/order_modify/protocols.py 的 OrderModifyService Protocol。
- 越权防护：所有写方法先调 OrderService.get_order(user_id, order_no) 校验归属
  （get_order 内部已做 user_id 过滤 + 软删过滤；不存在返 None → 抛 OrderNotFoundError）
- 状态校验：仅 MODIFIABLE_STATUSES（pending/paid）允许写
- 修改限制：分两种类型
  * modify_address  → UPDATE orders.shipping_address
  * modify_item_spec → UPDATE order_items.qty + subtotal（重算订单 total）
  * merge_orders     → 把被合并订单的 order_items 移入主订单 + 软删被合并订单

依赖（CLAUDE.md §7.2 分层）：
- 上游：OrderService Protocol（复用 S15 · 不直接 import mysql_impl）
- 客户端：mysql_client.with_safe_session（直接连 db 是允许的：infrastructure 层）
- 模型：Order + OrderItem ORM（models/order.py）
"""
import datetime
import logging
from typing import List, Optional

from app.clients.mysql_client import with_safe_session
from app.models.order import Order as OrderORM, OrderItem as OrderItemORM
from app.schemas.business import (
    MergeResult,
    ModifyNotAllowedError,
    ModifyResult,
    OrderNotFoundError,
    MergeConditionError,
)

logger = logging.getLogger(__name__)


def _now() -> datetime.datetime:
    """统一取「现在」（测试可 patch 替换为固定时间戳）

    注意：不要改为类属性或全局，保留 free function 便于 monkeypatch。
    """
    return datetime.datetime.now()


# =============================================================
# 状态校验辅助
# =============================================================
def _check_user_order(user_id: int, order_no: str):
    """越权 + 状态校验（OrderService 的强校验纯函数版本）

    返回 (OrderORM, OrderItemORM-列表) 元组。
    - 订单不存在/越权 → 抛 OrderNotFoundError
    - 状态不允许修改 → 抛 ModifyNotAllowedError
    """
    with with_safe_session(commit=False) as db:
        o = db.query(OrderORM).filter(
            OrderORM.order_no == order_no,
            OrderORM.user_id == user_id,      # 越权防护
            OrderORM.deleted == 0,
        ).first()
        if o is None:
            raise OrderNotFoundError(
                f"订单不存在或无权限访问：order_no={order_no}"
            )
        items = db.query(OrderItemORM).filter(
            OrderItemORM.order_id == o.id,
            OrderItemORM.deleted == 0,
        ).all()
        # 状态校验
        if o.status not in ("pending", "paid"):
            status_label = {
                "shipped": "已发货",
                "delivered": "已签收",
                "completed": "已完成",
                "refunded": "已退款",
            }.get(o.status, o.status)
            raise ModifyNotAllowedError(
                f"订单状态为「{status_label}」，不可修改"
            )
        return o, list(items)


# =============================================================
# OrderModifyService 默认实现
# =============================================================
class MySQLOrderModifyService:
    """OrderModifyService Protocol 的 MySQL 默认实现"""

    async def modify_address(
        self, user_id: int, order_no: str, new_address: str,
    ) -> ModifyResult:
        o, _ = _check_user_order(user_id, order_no)
        if not new_address or not new_address.strip():
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="address",
                reason="新地址不能为空",
            )
        old_addr = o.shipping_address
        with with_safe_session(commit=True) as db:
            o2 = db.query(OrderORM).filter(OrderORM.id == o.id).first()
            if o2 is None:
                # 并发删除了（defensive，不应发生）
                raise OrderNotFoundError(f"订单不存在：id={o.id}")
            o2.shipping_address = new_address.strip()
        return ModifyResult(
            success=True, order_no=order_no,
            modification_type="address",
            reason=f"地址已修改为「{new_address.strip()}」",
            before_snapshot={"shipping_address": old_addr},
            after_snapshot={"shipping_address": new_address.strip()},
        )

    async def modify_item_spec(
        self, user_id: int, order_no: str, sku: str, new_qty: Optional[int] = None,
    ) -> ModifyResult:
        o, items = _check_user_order(user_id, order_no)
        target = next((i for i in items if i.sku == sku and i.deleted == 0), None)
        if target is None:
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="spec",
                reason=f"SKU 「{sku}」不在该订单中",
            )
        # new_qty=None 表示不改数量
        if new_qty is None:
            return ModifyResult(
                success=True, order_no=order_no,
                modification_type="spec",
                reason="未指定新数量，无修改",
                before_snapshot={"sku": sku, "qty": target.qty},
                after_snapshot={"sku": sku, "qty": target.qty},
            )
        if new_qty <= 0:
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="spec",
                reason="新数量必须大于 0",
            )
        old_qty = target.qty
        old_subtotal = float(target.subtotal)
        unit_price = float(target.unit_price)
        new_subtotal = round(unit_price * new_qty, 2)
        delta = new_subtotal - old_subtotal
        with with_safe_session(commit=True) as db:
            item = db.query(OrderItemORM).filter(OrderItemORM.id == target.id).first()
            item.qty = new_qty
            item.subtotal = new_subtotal
            # 同步订单总金额
            order_row = db.query(OrderORM).filter(OrderORM.id == o.id).first()
            order_row.total_amount = round(float(order_row.total_amount) + delta, 2)
        return ModifyResult(
            success=True, order_no=order_no,
            modification_type="spec",
            reason=f"SKU 「{sku}」数量从 {old_qty} 调整为 {new_qty}",
            before_snapshot={"sku": sku, "qty": old_qty, "subtotal": old_subtotal},
            after_snapshot={"sku": sku, "qty": new_qty, "subtotal": new_subtotal},
        )

    async def merge_orders(
        self, user_id: int, order_nos: List[str],
    ) -> MergeResult:
        # 参数校验
        if not order_nos or len(order_nos) < 2:
            raise MergeConditionError("合并订单至少需要 2 个订单号")

        # 预校验所有订单存在 + 越权 + 未发货
        with with_safe_session(commit=False) as db:
            rows: List[OrderORM] = []
            for ono in order_nos:
                o = db.query(OrderORM).filter(
                    OrderORM.order_no == ono,
                    OrderORM.user_id == user_id,
                    OrderORM.deleted == 0,
                ).first()
                if o is None:
                    raise OrderNotFoundError(
                        f"订单不存在或无权限访问：order_no={ono}"
                    )
                if o.status not in ("pending", "paid"):
                    raise MergeConditionError(
                        f"订单 {ono} 状态为 {o.status}，不可合并"
                    )
                rows.append(o)

            # 条件 1：5 分钟时间窗（以最早订单 create_time 为锚）
            rows.sort(key=lambda r: r.create_time)
            earliest = rows[0]
            now = _now()
            window = datetime.timedelta(minutes=5)
            if now - earliest.create_time > window:
                raise MergeConditionError(
                    f"订单最早创建于 {earliest.create_time.isoformat()}，"
                    f"超过 {int(window.total_seconds() // 60)} 分钟，不可合并"
                )

            # 条件 2：同店（用 product_id 集合交集作为「同一店铺」近似；
            # V3+ 接入店铺表后用真实 store_id）
            primary = rows[0]
            items_by_order: dict[int, List[OrderItemORM]] = {}
            for r in rows:
                items = db.query(OrderItemORM).filter(
                    OrderItemORM.order_id == r.id,
                    OrderItemORM.deleted == 0,
                ).all()
                items_by_order[r.id] = list(items)

            primary_stores = {i.product_id for i in items_by_order[primary.id]}
            for r in rows[1:]:
                other_stores = {i.product_id for i in items_by_order[r.id]}
                if primary_stores & other_stores == set() and (primary_stores and other_stores):
                    # 至少两边都有商品，且交集为空 = 跨店（简化判定）
                    raise MergeConditionError(
                        f"订单 {primary.order_no} 与 {r.order_no} 不在同一店铺"
                    )
                # 合并店铺集合
                primary_stores |= other_stores

        # 通过所有条件 → 执行合并
        merged_ids: List[str] = [r.order_no for r in rows[1:]]
        with with_safe_session(commit=True) as db:
            for r in rows[1:]:
                # 把被合并订单的 items 移到主订单
                items = db.query(OrderItemORM).filter(
                    OrderItemORM.order_id == r.id,
                    OrderItemORM.deleted == 0,
                ).all()
                for it in items:
                    it.order_id = primary.id
                # 软删被合并订单
                row = db.query(OrderORM).filter(OrderORM.id == r.id).first()
                row.deleted = 1
                # 标记状态（业务可见：避免再次误合）
                # V2 简化：保留 status=pending/paid；deleted=1 即视为无效
            # 重算主订单总金额
            items = db.query(OrderItemORM).filter(
                OrderItemORM.order_id == primary.id,
                OrderItemORM.deleted == 0,
            ).all()
            new_total = round(sum(float(i.subtotal) for i in items), 2)
            main = db.query(OrderORM).filter(OrderORM.id == primary.id).first()
            main.total_amount = new_total

        return MergeResult(
            success=True,
            primary_order_no=primary.order_no,
            merged_order_nos=merged_ids,
            reason=f"已合并 {len(merged_ids)} 个订单到主订单 {primary.order_no}",
        )
