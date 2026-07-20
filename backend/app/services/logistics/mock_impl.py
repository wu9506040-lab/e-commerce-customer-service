"""
Mock 默认实现 — MockLogisticsService（Sprint 16）

实现 services/logistics/protocols.py 的 LogisticsService Protocol。

为什么是 Mock 而非 MySQL：当前 ORM 没有 logistics 表（V2 不持久化物流轨迹，
每次根据订单状态实时生成）。接入方真实实现（HTTP 调快递100 API）时自然不涉及此问题。

**关键决策**（spec §2.3）：
- Mock 不强制走 OrderService（避免 user_id 强依赖 + 循环依赖风险）
- Mock 直接查 Order ORM（基础设施层查 ORM 是允许的，CLAUDE.md §9.2.2）
- 物流查询不涉及 user 越权（订单号本身就是公开 token）
"""
import datetime
import logging
from typing import List

from app.clients.mysql_client import with_safe_session
from app.models.order import Order as OrderORM
from app.schemas.business import Logistics, TrackingEvent, TrackingInfo

logger = logging.getLogger(__name__)

# Mock 状态映射（与原 OrderTool.get_logistics 一致）
_STATUS_MAP = {
    "pending":   ("待发货",   "仓库"),
    "paid":      ("待发货",   "仓库"),
    "shipped":   ("运输中",   "深圳转运中心"),
    "delivered": ("已签收",   "北京海淀"),
    "completed": ("已签收",   "北京海淀"),
    "refunded":  ("已退回",   "深圳售后部"),
}

_CARRIERS = ["顺丰", "中通", "圆通", "韵达"]


class MockLogisticsService:
    """Mock 实现：基于订单状态生成 mock 物流信息（无持久化）"""

    async def query(self, order_no: str) -> Logistics | None:
        with with_safe_session(commit=False) as db:
            order = db.query(OrderORM).filter(
                OrderORM.order_no == order_no,
                OrderORM.deleted == 0,
            ).first()
            if not order:
                return None

            logistics_status, location = _STATUS_MAP.get(order.status, ("未知", "未知"))
            return Logistics(
                order_no=order_no,
                tracking_no=f"SF{order_no[3:]}",     # 与原 get_logistics mock 兼容
                carrier="顺丰",
                status=logistics_status,
                last_location=location,
                estimated_arrival=None,
            )

    async def track(self, tracking_no: str) -> TrackingInfo | None:
        """按运单号查完整轨迹

        反推 order_no（mock 约定：tracking_no = SF{order_no[3:]}），再调 query 拿订单状态。
        """
        if not tracking_no.startswith("SF"):
            return None
        # 从运单号反推 order_no（mock 约定）
        order_no = "ORD" + tracking_no[2:]
        with with_safe_session(commit=False) as db:
            order = db.query(OrderORM).filter(
                OrderORM.order_no == order_no,
                OrderORM.deleted == 0,
            ).first()
            if not order:
                return None

            events = _build_trajectory_events(order)
            status, _ = _STATUS_MAP.get(order.status, ("未知", "未知"))
            return TrackingInfo(
                tracking_no=tracking_no,
                carrier="顺丰",
                status=status,
                events=events,
            )

    async def get_carriers(self) -> List[str]:
        return list(_CARRIERS)


def _build_trajectory_events(order: OrderORM) -> List[TrackingEvent]:
    """根据订单状态生成 mock 轨迹事件（与原 OrderTool.get_logistics 一致）"""
    events: List[TrackingEvent] = []
    if order.status not in ("shipped", "delivered", "completed", "refunded"):
        return events  # pending / paid 不生成轨迹

    base = order.create_time or datetime.datetime.now()
    events.append(TrackingEvent(time=base, event="已下单"))
    events.append(TrackingEvent(
        time=base + datetime.timedelta(hours=2),
        event="已发货",
        location="深圳仓库",
    ))
    events.append(TrackingEvent(
        time=base + datetime.timedelta(days=1),
        event="运输中",
        location="广州转运中心",
    ))
    if order.status in ("delivered", "completed"):
        events.append(TrackingEvent(
            time=base + datetime.timedelta(days=2),
            event="已签收",
            location="北京海淀",
        ))
    if order.status == "refunded":
        events.append(TrackingEvent(
            time=base + datetime.timedelta(days=3),
            event="已退回",
            location="深圳售后部",
        ))
    return events