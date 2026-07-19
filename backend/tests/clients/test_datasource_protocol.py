"""
DataSource Protocol + StaticSeedSource 单测

按 CLAUDE.md §4.4 Stop-Loss 自检 ≥ 3 case；按 §7.3 接口就近放置。

测试策略（V1.1）：
  - Protocol 合规性测试：用 MockSource 验证 DataSourceProtocol 签名覆盖
  - dict schema 测试：验证返回字段齐全（防 schema drift）
  - StaticSeedSource 集成测试：db_session fixture + 真 ORM 行为（V1.2）

V1.1 暂只测协议层（schema 验证 + Protocol 合规）；
    StaticSeedSource 真 DB 集成测试 留 V1.2 升级路径（CI 真 MySQL）。
"""
from typing import AsyncIterator, List, Optional

import pytest

from app.clients.datasource.protocols import DataSourceProtocol
from app.clients.datasource.static_seed_source import StaticSeedSource


# =============================================================
# MockSource — 协议合规测试用
# =============================================================


class MockSource:
    """满足 DataSourceProtocol 的最小实现（覆盖所有签名）"""

    def __init__(self):
        self.products_call_count = 0
        self.orders_call_count = 0

    def fetch_products(self, *, sku=None, category=None, limit=20) -> List[dict]:
        self.products_call_count += 1
        return [{"sku": sku or "MOCK001", "name": "Mock 手机", "price": 1.0, "category": category or "mock"}]

    def search_products_by_keyword(self, keyword, *, limit=10) -> List[dict]:
        return [{"sku": "MOCK002", "name": f"Mock search={keyword}", "price": 2.0}]

    def fetch_orders(self, *, user_id, status=None, limit=20) -> List[dict]:
        self.orders_call_count += 1
        return [{"order_no": f"MOCK{user_id}", "status": status or "pending"}]

    def fetch_user_orders_with_logistics(self, *, user_id, order_no) -> Optional[dict]:
        return None

    def fetch_user_profile(self, *, user_id) -> Optional[dict]:
        return {"id": user_id, "username": f"mock_{user_id}"}

    async def subscribe_webhook(self, event_type) -> AsyncIterator[dict]:
        if False:  # never execute
            yield {"event": event_type}


# =============================================================
# Test 1: Protocol 合规性（runtime_checkable）
# =============================================================


def test_mock_source_satisfies_data_source_protocol():
    """Mock 实例必须被 isinstance(source, DataSourceProtocol) 识别"""
    source = MockSource()
    assert isinstance(source, DataSourceProtocol), (
        "MockSource 必须实现 DataSourceProtocol 所有方法签名"
        "（含 subscribe_webhook 的 async generator 签名）"
    )


def test_static_seed_source_satisfies_data_source_protocol():
    """StaticSeedSource 类本身必须满足 Protocol（即使没实例化也合规）"""
    # StaticSeedSource 全方法都是 @staticmethod，类对象也满足 Protocol 的方法集合
    assert hasattr(StaticSeedSource, "fetch_products")
    assert hasattr(StaticSeedSource, "fetch_orders")
    assert hasattr(StaticSeedSource, "fetch_user_profile")
    assert hasattr(StaticSeedSource, "search_products_by_keyword")
    assert hasattr(StaticSeedSource, "subscribe_webhook")


# =============================================================
# Test 2: Mock 实现行为正确性
# =============================================================


def test_mock_fetch_products_records_call():
    """fetch_products 必须按 keyword-only 参数解析"""
    source = MockSource()
    products = source.fetch_products(sku="ABC", limit=5)
    assert len(products) == 1
    assert products[0]["sku"] == "ABC"
    assert source.products_call_count == 1


def test_mock_fetch_orders_records_call():
    """fetch_orders 必须收 user_id 显式（防越权留痕）"""
    source = MockSource()
    orders = source.fetch_orders(user_id=42, status="shipped")
    assert orders[0]["order_no"] == "MOCK42"
    assert orders[0]["status"] == "shipped"
    assert source.orders_call_count == 1


def test_mock_user_profile_returns_id():
    """fetch_user_profile 必须按 user_id 返回 dict"""
    source = MockSource()
    profile = source.fetch_user_profile(user_id=7)
    assert profile == {"id": 7, "username": "mock_7"}


# =============================================================
# Test 3: StaticSeedSource 返回 schema 验证（不依赖 DB）
# =============================================================


def test_static_seed_source_subscribe_webhook_raises_not_implemented():
    """StaticSeedSource.subscribe_webhook 必须抛 NotImplementedError（M18+ 占位）"""
    import asyncio

    async def _check():
        with pytest.raises(NotImplementedError) as exc_info:
            async for _ in StaticSeedSource.subscribe_webhook("trade.create"):
                pass
        assert "StaticSeedSource" in str(exc_info.value)
        assert "TaobaoAdapter" in str(exc_info.value)

    asyncio.run(_check())


def test_static_seed_source_dict_field_schema():
    """_order_to_dict 必须包含 synthesize 节点注入 prompt 必需的字段

    防止 schema drift：M14 V3 期间 Order 字段被改后 _order_to_dict 漏字段
    导致下游 prompt 拼接错误。
    """
    from datetime import datetime
    from types import SimpleNamespace

    fake_order = SimpleNamespace(
        order_no="ORD-TEST-001",
        status="shipped",
        total_amount=3999.0,
        create_time=datetime(2026, 6, 15, 10, 30, 0),
    )
    result = StaticSeedSource._order_to_dict(fake_order)

    # 必需字段
    assert result["order_no"] == "ORD-TEST-001"
    assert result["status"] == "shipped"
    assert result["total_amount"] == 3999.0
    assert result["create_time"] == "2026-06-15T10:30:00"


def test_static_seed_source_product_to_dict_extracts_category_from_attributes():
    """_product_to_dict 必须从 attributes JSON 抽出 category（schema 约定）"""
    from types import SimpleNamespace

    # attributes 含 category
    p_with_attrs = SimpleNamespace(
        sku="SKU001", name="ZP1 手机", price=3999.0,
        stock=100, status=1,
        attributes={"category": "手机", "brand": "智选"},
    )
    result = StaticSeedSource._product_to_dict(p_with_attrs)
    assert result["category"] == "手机"
    assert result["sku"] == "SKU001"

    # attributes 为 None 时不能 crash
    p_no_attrs = SimpleNamespace(
        sku="SKU999", name="无 attrs", price=10.0,
        stock=1, status=1, attributes=None,
    )
    result2 = StaticSeedSource._product_to_dict(p_no_attrs)
    assert result2["category"] is None
    assert result2["sku"] == "SKU999"


# =============================================================
# Test 4: subscribe_webhook 占位的存在性（grep-friendly）
# =============================================================


def test_protocol_documents_webhook_as_m18_plus():
    """DataSourceProtocol.subscribe_webhook 文档必须明确写"M18+" 占位语义

    防止后人误把 subscribe_webhook 当作"已实现"删掉 NotImplementedError。
    """
    import inspect

    src = inspect.getsource(DataSourceProtocol.subscribe_webhook)
    assert "M18+" in src
    assert "NotImplementedError" in src or "占位" in src


# =============================================================
# Test 5: protocols.py 是 Protocol 不是 ABC（CLAUDE.md §9.3 鸭子类型）
# =============================================================


def test_data_source_protocol_is_protocol_not_abc():
    """DataSourceProtocol 必须是 typing.Protocol（结构子类型），不是 ABC

    CLAUDE.md §9.3 Interface First 要求：
    - Protocol = 鸭子类型（无需继承）
    - ABC = 必须显式继承（侵入性强）

    当前实现选择 Protocol 保持业务模块不被绑死。
    """
    from typing import Protocol

    assert issubclass(DataSourceProtocol, Protocol), (
        "DataSourceProtocol 必须是 typing.Protocol（CLAUDE.md §9.3 接口契约）"
    )
