"""
Sprint 16 · LogisticsService Protocol 契约测试（spec §2.6 · 7 用例）

策略：
- #1-#6 Mock 默认实现：patch mock_impl.with_safe_session → 真 SQLite session（seed 数据），
  验证不同订单状态 → 不同 Logistics / TrackingInfo 行为。
- #7 Tool 改用 Protocol：patch 工厂返回 mock Protocol，验证 Tool 委托而非直连 DB。

env 兜底由 conftest.py 提供（JWT_SECRET / DATABASE_URL）。
"""
import asyncio
import datetime
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.order import Order as OrderORM
from app.services.logistics import mock_impl
from app.services.logistics.mock_impl import MockLogisticsService
from app.services.logistics.protocols import LogisticsService


# =============================================================
# 真 SQLite session + seed（供 #1-#5）
# =============================================================
@pytest.fixture
def seeded_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    base = datetime.datetime(2026, 7, 1, 10, 0, 0)
    orders = [
        OrderORM(id=1, order_no="ORD001", user_id=1001, status="shipped",
                 total_amount=100, create_time=base, update_time=base),
        OrderORM(id=2, order_no="ORD002", user_id=1001, status="delivered",
                 total_amount=200, create_time=base, update_time=base),
        OrderORM(id=3, order_no="ORD003", user_id=1001, status="refunded",
                 total_amount=300, create_time=base, update_time=base),
    ]
    s.add_all(orders)
    s.commit()

    @contextmanager
    def fake_safe_session(commit=True):
        yield s

    with patch.object(mock_impl, "with_safe_session", fake_safe_session):
        yield s
    s.close()
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


# =============================================================
# #1-#4 MockLogisticsService.query
# =============================================================
def test_logistics_query_shipped(seeded_session):
    """#1 shipped → 运输中 + 深圳转运中心"""
    svc = MockLogisticsService()
    logistics = _run(svc.query("ORD001"))
    assert logistics is not None
    assert logistics.order_no == "ORD001"
    assert logistics.carrier == "顺丰"
    assert logistics.tracking_no == "SF001"        # SF{order_no[3:]} = SF001
    assert logistics.status == "运输中"
    assert logistics.last_location == "深圳转运中心"


def test_logistics_query_delivered(seeded_session):
    """#2 delivered → 已签收"""
    svc = MockLogisticsService()
    logistics = _run(svc.query("ORD002"))
    assert logistics is not None
    assert logistics.status == "已签收"
    assert logistics.last_location == "北京海淀"


def test_logistics_query_refunded(seeded_session):
    """#3 refunded → 已退回"""
    svc = MockLogisticsService()
    logistics = _run(svc.query("ORD003"))
    assert logistics is not None
    assert logistics.status == "已退回"
    assert logistics.last_location == "深圳售后部"


def test_logistics_query_not_found(seeded_session):
    """#4 订单不存在 → None"""
    svc = MockLogisticsService()
    assert _run(svc.query("NOPE")) is None


# =============================================================
# #5 MockLogisticsService.track
# =============================================================
def test_logistics_track(seeded_session):
    """#5 按运单号查完整轨迹"""
    svc = MockLogisticsService()
    info = _run(svc.track("SF001"))                # 对应 ORD001 (shipped)
    assert info is not None
    assert info.tracking_no == "SF001"
    assert info.carrier == "顺丰"
    assert info.status == "运输中"
    # shipped 状态有 3 个事件：已下单 / 已发货 / 运输中（无已签收 / 已退回）
    assert len(info.events) == 3
    events = [e.event for e in info.events]
    assert events == ["已下单", "已发货", "运输中"]
    # 非 SF 前缀 → None
    assert _run(svc.track("INVALID")) is None


# =============================================================
# #6 get_carriers
# =============================================================
def test_logistics_get_carriers(seeded_session):
    """#6 get_carriers 返 4 个快递公司"""
    svc = MockLogisticsService()
    carriers = _run(svc.get_carriers())
    assert carriers == ["顺丰", "中通", "圆通", "韵达"]


# =============================================================
# #7 Tool 改用 Protocol（mock 替换）
# =============================================================
def test_order_tool_logistics_uses_protocol():
    """#7 OrderTool.get_logistics 走 LogisticsService Protocol（mock 替换即生效）"""
    from app.schemas.business import Logistics as LogisticsDTO
    from app.tools.order_tool import OrderTool

    fake_logistics = LogisticsDTO(
        order_no="ORD001",
        tracking_no="SF001",
        carrier="顺丰",
        status="运输中",
        last_location="深圳转运中心",
        estimated_arrival=None,
    )
    mock_svc = AsyncMock()
    mock_svc.query.return_value = fake_logistics
    mock_factory = type("F", (), {"get_logistics_service": lambda self: mock_svc})()

    with patch("app.tools.order_tool.get_logistics_service_factory", return_value=mock_factory):
        result = OrderTool.get_logistics("ORD001")

    mock_svc.query.assert_awaited_once_with("ORD001")
    # dict 字段与旧 get_logistics 兼容（logistics_no = tracking_no）
    assert result == {
        "order_no": "ORD001",
        "logistics_no": "SF001",
        "status": "运输中",
        "last_location": "深圳转运中心",
        "trajectory": [],
    }


# =============================================================
# Factory 一致性（额外验证）
# =============================================================
def test_factory_returns_mock_impl():
    """get_logistics_service() → MockLogisticsService + runtime_checkable"""
    from app.services.logistics.factory import get_logistics_service_factory

    factory = get_logistics_service_factory()
    assert isinstance(factory.get_logistics_service(), MockLogisticsService)
    assert isinstance(factory.get_logistics_service(), LogisticsService)