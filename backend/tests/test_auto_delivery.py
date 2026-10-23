"""自动发货测试（V21 P0：AutoDeliveryTool + DeliveryService 端到端链路）。

覆盖：票据两步确认 / 越权拒绝 / 未支付拒绝 / 库存不足 / 正常发货 /
双层幂等（重复确认返回原卡密）/ 交付记录审计。
"""
from __future__ import annotations

import pytest

from app.services.agent_gating import reset_approval_gate
from app.core.config import settings
from app.services.delivery_service import DeliveryService, reset_delivery_service
from app.tools import auto_delivery_tool
from app.tools.auto_delivery_tool import deliver


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    """独立 SQLite（经 settings 指向 tmp，让单例也用同一库）+ 空门控 + mock 订单。"""
    monkeypatch.setattr(settings, "DELIVERY_DB", str(tmp_path / "delivery.db"), raising=False)
    reset_approval_gate()
    reset_delivery_service()
    d = DeliveryService(db_path=str(tmp_path / "delivery.db"))
    d.load_inventory("SKU1002", ["CARD-AAA-111", "CARD-BBB-222"])

    monkey_orders = {
        "ORD9": {"order_no": "ORD9", "status": "paid", "total_amount": 99.0},
        "ORD10": {"order_no": "ORD10", "status": "pending", "total_amount": 99.0},
    }
    orig = auto_delivery_tool.OrderTool.get_order_by_no
    auto_delivery_tool.OrderTool.get_order_by_no = staticmethod(
        lambda user_id, order_no: (
            monkey_orders.get(order_no) if user_id == 7 else None
        )
    )
    yield d
    auto_delivery_tool.OrderTool.get_order_by_no = orig
    reset_approval_gate()
    reset_delivery_service()


def test_step1_needs_confirmation_with_ticket():
    r = deliver(7, "ORD9", "SKU1002", confirmed=False)
    assert r["status"] == "needs_confirmation"
    assert r["ticket"]["status"] == "pending"
    assert r["order"]["status"] == "paid"


def test_step2_delivers_card():
    deliver(7, "ORD9", "SKU1002", confirmed=False)
    r = deliver(7, "ORD9", "SKU1002", confirmed=True)
    assert r["status"] == "delivered"
    assert r["card_secret"] in {"CARD-AAA-111", "CARD-BBB-222"}
    assert r["duplicate"] is False


def test_duplicate_confirm_returns_same_card():
    """双层幂等：重复确认发货 → 返回原卡密，不重复扣库存。"""
    deliver(7, "ORD9", "SKU1002", confirmed=False)
    r1 = deliver(7, "ORD9", "SKU1002", confirmed=True)
    r2 = deliver(7, "ORD9", "SKU1002", confirmed=True)
    assert r1["card_secret"] == r2["card_secret"]
    assert r2["duplicate"] is True


def test_not_owner_rejected():
    """user 8 查 user 7 的订单 → not_found（越权防护）。"""
    r = deliver(8, "ORD9", "SKU1002", confirmed=False)
    assert r["status"] == "order_not_found"


def test_unpaid_order_rejected():
    r = deliver(7, "ORD10", "SKU1002", confirmed=False)
    assert r["status"] == "order_not_paid"


def test_out_of_stock_rejected():
    r = deliver(7, "ORD9", "SKU-NO-STOCK", confirmed=False)
    assert r["status"] == "out_of_stock"


def test_delivery_record_written():
    deliver(7, "ORD9", "SKU1002", confirmed=False)
    deliver(7, "ORD9", "SKU1002", confirmed=True)
    rec = auto_delivery_tool.get_delivery_service().get_record("ORD9")
    assert rec is not None
    assert rec["order_no"] == "ORD9"
    assert rec["sku"] == "SKU1002"
