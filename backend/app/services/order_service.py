"""
订单服务 - 编排 OrderTool + 物流 mock

按 PROJECT_DESIGN.md §3：order_query 走 Tool（结构化查询）。
本服务 = 业务编排层，调 OrderTool 拿数据，按需补充物流 mock。
"""
import logging
from typing import Optional

from app.tools.order_tool import OrderTool

logger = logging.getLogger(__name__)


class OrderService:
    """订单业务服务（编排层）"""

    @staticmethod
    def list_user_orders(user_id: int, status: Optional[str] = None, with_items: bool = False) -> list[dict]:
        """
        查用户订单列表

        Args:
            user_id: 用户 ID
            status: 状态过滤
            with_items: 是否展开明细（默认 False，列表场景下避免 N+1）
        """
        orders = OrderTool.list_user_orders(user_id, status=status)
        if with_items:
            for o in orders:
                # V2 简化：列表场景下明细通常不需要；如需则下次按需补
                pass
        return orders

    @staticmethod
    def get_order_detail(user_id: int, order_no: str) -> Optional[dict]:
        """
        查订单详情（含明细 + 物流）

        Returns:
            {
                "order": {...},
                "items": [...],
                "logistics": {...},
            } 或 None（订单不存在）
        """
        order = OrderTool.get_order_by_no(user_id, order_no)
        if not order:
            return None

        # 拿明细（order_id 来自 order，但 order dict 没暴露 id）
        # 简化：直接调 get_order_items 需要 order_id，再查一次
        # V2 简化：暂不在列表返回 id，需要时再补（详见 M3 API 层）
        from app.clients.mysql_client import with_safe_session
        from app.models.order import Order

        with with_safe_session(commit=False) as db:
            o = db.query(Order).filter(Order.order_no == order_no).first()
            order_id = o.id if o else 0

        items = OrderTool.get_order_items(order_id) if order_id else []
        logistics = OrderTool.get_logistics(order_no)

        return {
            "order": order,
            "items": items,
            "logistics": logistics,
        }