"""
Sprint 18-C · OrderModifyService Protocol 契约测试（spec §4.5 · 8 用例）

策略（参考 S15 test_order_protocol.py）：
- 真 SQLite in-memory + patch mysql_impl.with_safe_session → 真 session 注入 seed 数据
- 验证 ORM 写、越权防护、状态限制、合并条件（5 分钟时间窗）
- 用 patch 替换 _now() 固定"当前时间"，确保 5 分钟时间窗不依赖运行时

env 兜底由 conftest.py 提供。
"""
import asyncio
import datetime
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.order import Order as OrderORM, OrderItem as OrderItemORM
from app.services.order_modify import mysql_impl as impl_module
from app.services.order_modify.mysql_impl import MySQLOrderModifyService
from app.services.order_modify.protocols import OrderModifyService
from app.schemas.business import (
    MergeConditionError,
    ModifyNotAllowedError,
    ModifyResult,
    OrderNotFoundError,
)


# =============================================================
# 真 SQLite session + seed + 固定 now
# =============================================================
@pytest.fixture
def seeded_session():
    """种子数据：用绝对 base_time + 偏移；fixture 注入 fake _now 固定 now=base_time。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    base = datetime.datetime(2026, 7, 1, 10, 0, 0)
    now = base  # 测试中所有"现在" = base

    orders = [
        # user 1001
        OrderORM(id=1, order_no="ORD001", user_id=1001, status="pending",
                 total_amount=100, shipping_address="旧地址 A",
                 create_time=base - datetime.timedelta(minutes=3),
                 update_time=base - datetime.timedelta(minutes=3)),
        # ORD002 与 ORD001 同 product=1（店铺 A），进入 5min 窗 — 用于 #6 同店合并测试
        OrderORM(id=2, order_no="ORD002", user_id=1001, status="paid",
                 total_amount=200, shipping_address="旧地址 B",
                 create_time=base - datetime.timedelta(minutes=2),
                 update_time=base - datetime.timedelta(minutes=2)),
        # ORD003 已发货（shipped）— 用于 #2 shipped 失败测试
        OrderORM(id=3, order_no="ORD003", user_id=1001, status="shipped",
                 total_amount=80, shipping_address="旧地址 C",
                 create_time=base - datetime.timedelta(minutes=5),
                 update_time=base - datetime.timedelta(minutes=5)),
        # ORD004 同 product=1（与 ORD001/ORD002 同店），4 分钟前，5min 窗内 — 可合并测试用
        OrderORM(id=4, order_no="ORD004", user_id=1001, status="paid",
                 total_amount=150, shipping_address="旧地址 D",
                 create_time=base - datetime.timedelta(minutes=4),
                 update_time=base - datetime.timedelta(minutes=4)),
        # ORD005 product=4（店铺 B），2 分钟前 — 跨店测试
        OrderORM(id=5, order_no="ORD005", user_id=1001, status="pending",
                 total_amount=60, shipping_address="旧地址 E",
                 create_time=base - datetime.timedelta(minutes=2),
                 update_time=base - datetime.timedelta(minutes=2)),
        # ORD006 product=5（店铺 C），1 分钟前 — 跨店测试（与 ORD005）
        OrderORM(id=6, order_no="ORD006", user_id=1001, status="pending",
                 total_amount=40, shipping_address="旧地址 F",
                 create_time=base - datetime.timedelta(minutes=1),
                 update_time=base - datetime.timedelta(minutes=1)),
        # ORD_OTHER 属 user 2002 — 越权测试
        OrderORM(id=9, order_no="ORD_OTHER", user_id=2002, status="paid",
                 total_amount=50, shipping_address="他人地址",
                 create_time=base, update_time=base),
        # ORD_OLD 30 分钟前（远超 5 分钟）— 合并时间窗失败测试
        OrderORM(id=8, order_no="ORD_OLD", user_id=1001, status="paid",
                 total_amount=100, shipping_address="旧地址 H",
                 create_time=base - datetime.timedelta(minutes=30),
                 update_time=base - datetime.timedelta(minutes=30)),
    ]
    items = [
        OrderItemORM(id=1, order_id=1, product_id=1, sku="SKU1",
                     product_name="商品1", qty=2, unit_price=50, subtotal=100),
        OrderItemORM(id=2, order_id=2, product_id=2, sku="SKU2",
                     product_name="商品2", qty=1, unit_price=200, subtotal=200),
        OrderItemORM(id=3, order_id=3, product_id=3, sku="SKU3",
                     product_name="商品3", qty=1, unit_price=80, subtotal=80),
        OrderItemORM(id=4, order_id=4, product_id=1, sku="SKU1",
                     product_name="商品1", qty=3, unit_price=50, subtotal=150),
        # ORD005 商品 4 — "店铺 B"
        OrderItemORM(id=5, order_id=5, product_id=4, sku="SKU4",
                     product_name="商品4", qty=1, unit_price=60, subtotal=60),
        # ORD006 商品 5 — "店铺 C"（与 ORD005 product 不同）
        OrderItemORM(id=6, order_id=6, product_id=5, sku="SKU5",
                     product_name="商品5", qty=1, unit_price=40, subtotal=40),
        OrderItemORM(id=9, order_id=9, product_id=9, sku="SKU_OTHER",
                     product_name="他人商品", qty=1, unit_price=50, subtotal=50),
        OrderItemORM(id=8, order_id=8, product_id=1, sku="SKU1",
                     product_name="商品1", qty=2, unit_price=50, subtotal=100),
    ]
    s.add_all(orders + items)
    s.commit()

    @contextmanager
    def fake_safe_session(commit=True):
        yield s

    # 固定 now + with_safe_session：合并测试时间窗可预期
    with patch.object(impl_module, "_now", lambda: now), \
         patch.object(impl_module, "with_safe_session", fake_safe_session):
        yield s
    s.close()
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


# =============================================================
# #1 modify_address pending 成功
# =============================================================
def test_modify_address_pending_success(seeded_session):
    """#1 pending 状态改地址 → success=True + reason 含中文 + before/after 快照"""
    svc = MySQLOrderModifyService()
    result = _run(svc.modify_address(1001, "ORD001", "新地址 X"))
    assert isinstance(result, ModifyResult)
    assert result.success is True
    assert result.order_no == "ORD001"
    assert result.modification_type == "address"
    assert "新地址 X" in result.reason
    assert result.before_snapshot == {"shipping_address": "旧地址 A"}
    assert result.after_snapshot == {"shipping_address": "新地址 X"}

    # DB 真改值
    o = seeded_session.query(OrderORM).filter(OrderORM.order_no == "ORD001").first()
    assert o.shipping_address == "新地址 X"


# =============================================================
# #2 modify_address shipped 失败
# =============================================================
def test_modify_address_shipped_fail(seeded_session):
    """#2 shipped 状态改地址 → 抛 ModifyNotAllowedError"""
    svc = MySQLOrderModifyService()
    with pytest.raises(ModifyNotAllowedError) as exc:
        _run(svc.modify_address(1001, "ORD003", "试改"))
    assert "不可修改" in str(exc.value)


# =============================================================
# #3 modify_address 越权
# =============================================================
def test_modify_address_unauthorized(seeded_session):
    """#3 user_id 不匹配 → 抛 OrderNotFoundError（防越权）"""
    svc = MySQLOrderModifyService()
    with pytest.raises(OrderNotFoundError):
        _run(svc.modify_address(1001, "ORD_OTHER", "X"))  # ORD_OTHER 属 2002


# =============================================================
# #4 modify_item_spec pending + qty 调整成功
# =============================================================
def test_modify_item_spec_pending_qty_change(seeded_session):
    """#4 pending + qty 调整 → success + qty/subtotal/order.total_amount 已更新"""
    svc = MySQLOrderModifyService()
    # ORD001 当前 qty=2, subtotal=100, unit=50；改成 qty=3 → subtotal=150；order total 100 → 150
    result = _run(svc.modify_item_spec(1001, "ORD001", "SKU1", new_qty=3))
    assert result.success is True
    assert result.modification_type == "spec"
    assert "3" in result.reason
    assert result.before_snapshot == {"sku": "SKU1", "qty": 2, "subtotal": 100.0}
    assert result.after_snapshot == {"sku": "SKU1", "qty": 3, "subtotal": 150.0}

    item = seeded_session.query(OrderItemORM).filter(
        OrderItemORM.sku == "SKU1", OrderItemORM.order_id == 1,
    ).first()
    assert item.qty == 3
    assert float(item.subtotal) == 150.0

    order = seeded_session.query(OrderORM).filter(OrderORM.order_no == "ORD001").first()
    assert float(order.total_amount) == 150.0


# =============================================================
# #5 modify_item_spec 不存在的 SKU
# =============================================================
def test_modify_item_spec_sku_not_found(seeded_session):
    """#5 SKU 不在该订单 → success=False + reason 含「不在」"""
    svc = MySQLOrderModifyService()
    result = _run(svc.modify_item_spec(1001, "ORD001", "SKU_NONEXIST", new_qty=5))
    assert result.success is False
    assert result.modification_type == "spec"
    assert "SKU_NONEXIST" in result.reason
    assert "不在" in result.reason


# =============================================================
# #6 merge_orders 同店 + 未发货 + 5 分钟内
# =============================================================
def test_merge_orders_same_store_within_5min(seeded_session):
    """#6 ORD001（3分钟前, product=1）+ ORD004（4分钟前, product=1）：同店 + 5min 窗 + 未发货

    排序后：ORD004 (4min前, 最早) → ORD001 (3min前) — ORD004 是主订单。
    """
    svc = MySQLOrderModifyService()
    result = _run(svc.merge_orders(1001, ["ORD001", "ORD004"]))
    assert result.success is True
    assert result.primary_order_no == "ORD004"  # 4 分钟前更早 → 最早 = 主
    assert "ORD001" in result.merged_order_nos

    # 主订单 items 已合并（SKU1 × 2 行：原 ORD004 一件 + ORD001 一件合并过来）
    items = seeded_session.query(OrderItemORM).filter(
        OrderItemORM.order_id == 4, OrderItemORM.deleted == 0,
    ).all()
    assert len(items) == 2  # 一行 order_id=4 (原 ORD004) + 1 行（从 ORD001 合并过来）

    # 主订单总金额：150 + 100 = 250
    main = seeded_session.query(OrderORM).filter(OrderORM.order_no == "ORD004").first()
    assert float(main.total_amount) == 250.0

    # 被合并订单软删
    o1 = seeded_session.query(OrderORM).filter(OrderORM.order_no == "ORD001").first()
    assert o1.deleted == 1


# =============================================================
# #7 merge_orders 跨店失败
# =============================================================
def test_merge_orders_cross_store_fail(seeded_session):
    """#7 跨店 → 抛 MergeConditionError（ORD005 product=4 vs ORD006 product=5）"""
    svc = MySQLOrderModifyService()
    # ORD005 (2min, product=4) + ORD006 (1min, product=5)：两店产品不同 → 跨店
    with pytest.raises(MergeConditionError) as exc:
        _run(svc.merge_orders(1001, ["ORD005", "ORD006"]))
    assert "同一店铺" in str(exc.value)


# =============================================================
# #8 merge_orders 超过 5 分钟
# =============================================================
def test_merge_orders_exceed_5min_fail(seeded_session):
    """#8 最早订单超过 5 分钟 → 抛 MergeConditionError（ORD_OLD 30 分钟前 + ORD001 3 分钟前）"""
    svc = MySQLOrderModifyService()
    with pytest.raises(MergeConditionError) as exc:
        _run(svc.merge_orders(1001, ["ORD_OLD", "ORD001"]))
    assert "超过" in str(exc.value)


# =============================================================
# Factory 验证（额外 · 不计入 8）
# =============================================================
def test_factory_returns_mysql_impl():
    """Factory.get_order_modify_service() → MySQLOrderModifyService（Protocol 一致性）"""
    from app.services.order_modify.factory import get_order_modify_service_factory

    factory = get_order_modify_service_factory()
    svc = factory.get_order_modify_service()
    assert isinstance(svc, MySQLOrderModifyService)
    assert isinstance(svc, OrderModifyService)

