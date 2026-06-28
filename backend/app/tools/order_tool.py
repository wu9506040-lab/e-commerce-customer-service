"""
订单 Tool - 纯 DB 查询 + 物流 mock

按 CLAUDE.md §6：tool 层只做 DB 查询 / 外部 API，不调 LLM 不做 RAG。
按 PROJECT_DESIGN.md §6：所有订单查询必须显式收 user_id，防越权。
"""
import datetime
from typing import Optional

from app.clients.mysql_client import with_safe_session
from app.models.order import Order, OrderItem


class OrderTool:
    """订单查询工具（静态方法集合；类形式方便未来扩展为单例 / 依赖注入）"""

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
        with with_safe_session(commit=False) as db:
            q = db.query(Order).filter(Order.user_id == user_id, Order.deleted == 0)
            if status:
                q = q.filter(Order.status == status)
            orders = q.order_by(Order.create_time.desc()).limit(limit).all()
            return [OrderTool._order_to_dict(o) for o in orders]

    @staticmethod
    def get_order_by_no(user_id: int, order_no: str) -> Optional[dict]:
        """
        按订单号查订单（强制 user_id 防越权）

        Returns:
            订单 dict 或 None（订单不存在 / 不属于该 user）
        """
        with with_safe_session(commit=False) as db:
            order = db.query(Order).filter(
                Order.order_no == order_no,
                Order.user_id == user_id,  # 越权防护
                Order.deleted == 0,
            ).first()
            return OrderTool._order_to_dict(order) if order else None

    # ---------- 订单明细 ----------

    @staticmethod
    def get_order_items(order_id: int) -> list[dict]:
        """查订单明细（order_id 来自上一级调用，已校验过归属）"""
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
        # 先查 order 拿 status
        with with_safe_session(commit=False) as db:
            order = db.query(Order).filter(
                Order.order_no == order_no,
                Order.deleted == 0,
            ).first()
            if not order:
                return {
                    "order_no": order_no,
                    "logistics_no": None,
                    "status": "订单不存在",
                    "last_location": None,
                    "trajectory": [],
                }

            logistics_no = f"SF{order_no[3:]}"  # 简化：从订单号生成 mock 单号
            status_map = {
                "pending":   ("待发货",   "仓库"),
                "paid":      ("待发货",   "仓库"),
                "shipped":   ("运输中",   "深圳转运中心"),
                "delivered": ("已签收",   "北京海淀"),
                "completed": ("已签收",   "北京海淀"),
                "refunded":  ("已退回",   "深圳售后部"),
            }
            logistics_status, location = status_map.get(order.status, ("未知", "未知"))

            trajectory = []
            if order.status in ("shipped", "delivered", "completed", "refunded"):
                trajectory = [
                    {"time": order.create_time.isoformat(), "event": "已下单"},
                    {"time": (order.create_time + datetime.timedelta(hours=2)).isoformat(), "event": "已发货", "location": "深圳仓库"},
                    {"time": (order.create_time + datetime.timedelta(days=1)).isoformat(), "event": "运输中", "location": "广州转运中心"},
                ]
            if order.status in ("delivered", "completed"):
                trajectory.append({
                    "time": (order.create_time + datetime.timedelta(days=2)).isoformat(),
                    "event": "已签收",
                    "location": "北京海淀",
                })
            if order.status == "refunded":
                trajectory.append({
                    "time": (order.create_time + datetime.timedelta(days=3)).isoformat(),
                    "event": "已退回",
                    "location": "深圳售后部",
                })

            return {
                "order_no": order_no,
                "logistics_no": logistics_no,
                "status": logistics_status,
                "last_location": location,
                "trajectory": trajectory,
            }

    # ---------- 私有 ----------

    @staticmethod
    def _order_to_dict(o: Order) -> dict:
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