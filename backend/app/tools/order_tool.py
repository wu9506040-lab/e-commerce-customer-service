"""
订单 Tool - 纯 DB 查询 + 物流 mock（走 Protocol）

按 CLAUDE.md §6：tool 层只做 DB 查询 / 外部 API，不调 LLM 不做 RAG。
按 PROJECT_DESIGN.md §6：所有订单查询必须显式收 user_id，防越权。
"""
import datetime
from typing import Optional

from app.clients.mysql_client import with_safe_session
from app.models.order import Order, OrderItem
from app.services.logistics.factory import get_logistics_service_factory
from app.services.order.factory import get_order_service_factory, run_sync

# Sprint 15：订单/商品读取改走 OrderService Protocol（CLAUDE.md §9.3.2）。
# Sprint 16：物流查询改走 LogisticsService Protocol。
# 越权防护 / 状态过滤 / 软删过滤等语义下沉到 MySQLOrderService，Tool 层只做 DTO→dict 适配。


class OrderTool:
    """订单查询工具（静态方法集合；DB 访问经 OrderService Protocol）"""

    # ---------- 订单主表 ----------

    @staticmethod
    def list_user_orders(user_id: int, status: Optional[str] = None, limit: int = 20) -> list[dict]:
        """
        查某用户的所有订单（可选按状态过滤）

        Args:
            user_id: 用户 ID（必传）
            status: 订单状态过滤（pending/paid/shipped/delivered/completed/refunded）
            limit: 最大返回数

        Returns:
            订单 dict 列表（按 create_time DESC）
        """
        svc = get_order_service_factory().get_order_service()
        orders, _ = run_sync(svc.list_user_orders(user_id, status=status, limit=limit))
        return [OrderTool._order_dto_to_dict(o) for o in orders]

    @staticmethod
    def get_order_by_no(user_id: int, order_no: str) -> Optional[dict]:
        """
        按订单号查订单（强制 user_id 防越权）

        Returns:
            订单 dict 或 None（订单不存在 / 不属于该 user）
        """
        svc = get_order_service_factory().get_order_service()
        order = run_sync(svc.get_order(user_id, order_no))
        return OrderTool._order_dto_to_dict(order) if order else None

    # ---------- 订单明细 ----------

    @staticmethod
    def get_order_items(order_id: int) -> list[dict]:
        """查订单明细（order_id 来自上一级调用，已校验过归属）

        note：Protocol 的 Order.items 按 order_no 聚合；本方法保留 order_id 入参
        （多个调用方依赖），故仍直接查 order_items 表。
        """
        with with_safe_session(commit=False) as db:
            items = db.query(OrderItem).filter(
                OrderItem.order_id == order_id,
                OrderItem.deleted == 0,
            ).all()
            return [OrderTool._item_to_dict(i) for i in items]

    # ---------- 物流 mock ----------

    @staticmethod
    def get_logistics(order_no: str) -> dict:
        """
        物流轨迹（V2 mock）

        根据订单状态硬编码返回物流信息：
        - pending / paid → 未发货
        - shipped → 运输中
        - delivered → 已签收
        - completed → 已签收
        - refunded → 已退回

        V3+ 替换为真实 API 调用（顺丰/菜鸟）
        """
        svc = get_logistics_service_factory().get_logistics_service()
        logistics = run_sync(svc.query(order_no))
        if logistics is None:
            return {
                "order_no": order_no,
                "logistics_no": None,
                "status": "订单不存在",
                "last_location": None,
                "trajectory": [],
            }
        return OrderTool._logistics_dto_to_dict(logistics)

    @staticmethod
    def _logistics_dto_to_dict(l) -> dict:
        """LogisticsService.Logistics (DTO) → dict（与旧 get_logistics 字段/格式一致）"""
        return {
            "order_no": l.order_no,
            "logistics_no": l.tracking_no,
            "status": l.status,
            "last_location": l.last_location,
            "trajectory": [],  # V2 Tool 层不展开轨迹；如需轨迹，调用方调 LogisticsService.track
        }

    # ---------- 私有 ----------

    @staticmethod
    def _order_dto_to_dict(o) -> dict:
        """OrderService.Order (DTO) → dict（与旧 _order_to_dict 字段/格式一致）"""
        return {
            "order_no": o.order_no,
            "status": o.status,
            "total_amount": float(o.total_amount),
            "create_time": o.create_time.isoformat() if o.create_time else None,
        }

    @staticmethod
    def _item_to_dict(i: OrderItem) -> dict:
        return {
            "sku": i.sku,
            "product_name": i.product_name,
            "qty": i.qty,
            "unit_price": float(i.unit_price),
            "subtotal": float(i.subtotal),
        }