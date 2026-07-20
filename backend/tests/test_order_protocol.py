"""
Sprint 15 · OrderService + ProductService Protocol 契约测试（spec §2.4 · 10 用例）

策略：
- #1-#8 MySQL 默认实现：patch mysql_impl.with_safe_session → 真 SQLite session（seed 数据），
  验证 ORM→DTO 映射 / 越权过滤 / 分页 cursor / LIKE 搜索 / 推荐。
- #9-#10 Tool 改用 Protocol：patch 工厂返回 mock Protocol，验证 Tool 委托而非直连 DB。

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
from app.models.order import Order as OrderORM, OrderItem as OrderItemORM, OrderStatus
from app.models.product import Product as ProductORM
from app.services.order import mysql_impl
from app.services.order.mysql_impl import MySQLOrderService, MySQLProductService
from app.services.order.protocols import OrderService, ProductService


# =============================================================
# 真 SQLite session + seed（供 #1-#8）
# =============================================================
@pytest.fixture
def seeded_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    now = datetime.datetime(2026, 7, 1, 10, 0, 0)
    # 用户 1001 三笔订单（不同状态 + 时间递增），用户 2002 一笔（越权用）
    orders = [
        OrderORM(id=1, order_no="ORD001", user_id=1001, status="paid",
                 total_amount=100, create_time=now, update_time=now),
        OrderORM(id=2, order_no="ORD002", user_id=1001, status="shipped",
                 total_amount=200, create_time=now + datetime.timedelta(days=1),
                 update_time=now + datetime.timedelta(days=1)),
        OrderORM(id=3, order_no="ORD003", user_id=1001, status="completed",
                 total_amount=300, create_time=now + datetime.timedelta(days=2),
                 update_time=now + datetime.timedelta(days=2)),
        OrderORM(id=9, order_no="ORD_OTHER", user_id=2002, status="paid",
                 total_amount=50, create_time=now, update_time=now),
    ]
    items = [
        OrderItemORM(id=1, order_id=1, product_id=1, sku="SKU1",
                     product_name="手机A", qty=2, unit_price=50, subtotal=100),
    ]
    products = [
        ProductORM(id=1, sku="SKU1", name="ZP1 旗舰手机", price=3999, stock=10,
                   status=1, description="旗舰", attributes={"color": "黑"}),
        ProductORM(id=2, sku="SKU2", name="ZP2 千元手机", price=1999, stock=5,
                   status=1, description="性价比", attributes={"color": "白"}),
        ProductORM(id=3, sku="SKU3", name="降噪耳机", price=699, stock=0,
                   status=1, description="耳机", attributes=None),
        ProductORM(id=4, sku="SKU4", name="下架手机", price=1, stock=0,
                   status=0, description="下架", attributes=None),  # status=0 不在售
    ]
    s.add_all(orders + items + products)
    s.commit()

    @contextmanager
    def fake_safe_session(commit=True):
        yield s

    with patch.object(mysql_impl, "with_safe_session", fake_safe_session):
        yield s
    s.close()
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


# =============================================================
# #1-#4 OrderService MySQL 实现
# =============================================================
def test_order_mysql_impl_get_order(seeded_session):
    """#1 mock session → Order 字段全对（含 items 映射 qty→quantity）"""
    svc = MySQLOrderService()
    order = _run(svc.get_order(1001, "ORD001"))
    assert order is not None
    assert order.order_no == "ORD001"
    assert order.user_id == 1001
    assert order.status == "paid"
    assert order.total_amount == 100.0
    assert len(order.items) == 1
    assert order.items[0].sku == "SKU1"
    assert order.items[0].quantity == 2          # ORM qty → DTO quantity
    assert order.items[0].subtotal == 100.0


def test_order_mysql_impl_get_order_not_found(seeded_session):
    """#2 订单不存在 → None；越权（他人订单）→ None"""
    svc = MySQLOrderService()
    assert _run(svc.get_order(1001, "NOPE")) is None
    # 越权：ORD_OTHER 属于 2002，用 1001 查应为 None
    assert _run(svc.get_order(1001, "ORD_OTHER")) is None


def test_order_mysql_impl_list_user_orders(seeded_session):
    """#3 多订单 + cursor 分页（DESC 排序 + next_cursor）"""
    svc = MySQLOrderService()
    page1, cursor = _run(svc.list_user_orders(1001, limit=2))
    assert [o.order_no for o in page1] == ["ORD003", "ORD002"]  # create_time DESC
    assert cursor is not None
    page2, cursor2 = _run(svc.list_user_orders(1001, limit=2, cursor=cursor))
    assert [o.order_no for o in page2] == ["ORD001"]
    assert cursor2 is None                                       # 无更多


def test_order_mysql_impl_filter_by_status(seeded_session):
    """#4 status 过滤生效"""
    svc = MySQLOrderService()
    orders, _ = _run(svc.list_user_orders(1001, status="shipped"))
    assert len(orders) == 1
    assert orders[0].order_no == "ORD002"
    # get_order_status 旁路验证
    assert _run(svc.get_order_status("ORD002")) == "shipped"
    assert _run(svc.get_order_status("NOPE")) is None


# =============================================================
# #5-#7 ProductService MySQL 实现
# =============================================================
def test_product_mysql_impl_get_product(seeded_session):
    """#5 mock session → Product 全字段（含 attributes 保留）"""
    svc = MySQLProductService()
    p = _run(svc.get_product("SKU1"))
    assert p is not None
    assert p.sku == "SKU1"
    assert p.name == "ZP1 旗舰手机"
    assert p.price == 3999.0
    assert p.stock == 10
    assert p.attributes == {"color": "黑"}
    # 下架商品（status=0）不返回
    assert _run(svc.get_product("SKU4")) is None


def test_product_mysql_impl_search_products(seeded_session):
    """#6 LIKE 搜索 + limit"""
    svc = MySQLProductService()
    hits = _run(svc.search_products("手机", limit=10))
    skus = {p.sku for p in hits}
    assert skus == {"SKU1", "SKU2"}          # 两款在售手机（下架的 SKU4 排除）
    # limit 生效
    limited = _run(svc.search_products("手机", limit=1))
    assert len(limited) == 1


def test_product_mysql_impl_get_recommendations(seeded_session):
    """#7 基于 context_skus 推荐（排除上下文已含 SKU）"""
    svc = MySQLProductService()
    recs = _run(svc.get_recommendations(1001, context_skus=["SKU1"], limit=5))
    rec_skus = {p.sku for p in recs}
    assert "SKU1" not in rec_skus            # 已在上下文，排除
    assert "SKU2" in rec_skus and "SKU3" in rec_skus
    assert "SKU4" not in rec_skus            # 下架不推荐


# =============================================================
# #8 Factory
# =============================================================
def test_factory_returns_mysql_impl():
    """#8 get_order_service() → MySQLOrderService；get_product_service() → MySQLProductService"""
    from app.services.order.factory import get_order_service_factory

    factory = get_order_service_factory()
    assert isinstance(factory.get_order_service(), MySQLOrderService)
    assert isinstance(factory.get_product_service(), MySQLProductService)
    # 协议 runtime_checkable 一致性
    assert isinstance(factory.get_order_service(), OrderService)
    assert isinstance(factory.get_product_service(), ProductService)


# =============================================================
# #9-#10 Tool 改用 Protocol（mock 替换）
# =============================================================
def test_refund_tool_uses_protocol():
    """#9 RefundTool.check_refundable 走 OrderService Protocol（mock 替换即生效）"""
    from app.schemas.business import Order as OrderDTO
    from app.tools.refund_tool import RefundTool

    now = datetime.datetime.now()
    fake_order = OrderDTO(
        order_no="ORDX", user_id=1001, status=OrderStatus.PAID.value,
        items=[], total_amount=100, create_time=now, update_time=now,
    )
    mock_svc = AsyncMock()
    mock_svc.get_order.return_value = fake_order
    mock_factory = type("F", (), {"get_order_service": lambda self: mock_svc})()

    with patch("app.tools.refund_tool.get_order_service_factory", return_value=mock_factory):
        result = RefundTool.check_refundable(1001, "ORDX")

    mock_svc.get_order.assert_awaited_once_with(1001, "ORDX")   # 委托给 Protocol
    assert result["refundable"] is True
    assert result["order_status"] == "paid"
    assert result["order_no"] == "ORDX"


def test_order_tool_uses_protocol():
    """#10 OrderTool.get_order_by_no 走 OrderService Protocol（mock 替换即生效）"""
    from app.schemas.business import Order as OrderDTO
    from app.tools.order_tool import OrderTool

    now = datetime.datetime(2026, 7, 1, 10, 0, 0)
    fake_order = OrderDTO(
        order_no="ORDY", user_id=1001, status="shipped",
        items=[], total_amount=250, create_time=now, update_time=now,
    )
    mock_svc = AsyncMock()
    mock_svc.get_order.return_value = fake_order
    mock_factory = type("F", (), {"get_order_service": lambda self: mock_svc})()

    with patch("app.tools.order_tool.get_order_service_factory", return_value=mock_factory):
        result = OrderTool.get_order_by_no(1001, "ORDY")

    mock_svc.get_order.assert_awaited_once_with(1001, "ORDY")   # 委托给 Protocol
    assert result == {
        "order_no": "ORDY",
        "status": "shipped",
        "total_amount": 250.0,
        "create_time": "2026-07-01T10:00:00",
    }
