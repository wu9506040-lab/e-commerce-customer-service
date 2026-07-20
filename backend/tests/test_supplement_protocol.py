"""
Sprint 20 通用客服中台 · SupplementRuleService Protocol 契约测试（spec §2.4 · 10 用例）

策略：
- 纯 YAML 查询方法（get_shipping_fee, get_purchase_limit_info, schedule_pickup）：
  注入 config dict；不依赖 OrderService。
- 需 OrderService 的方法（urge_shipment, extend_receipt）：mock OrderService 返 Order DTO。

env 兜底由 conftest.py 提供（JWT_SECRET / DATABASE_URL）。
"""
import asyncio
import datetime
from typing import Any, Dict, List, Optional

import pytest

from app.schemas.business import Order as OrderDTO
from app.services.supplement.protocols import (
    SupplementError,
    SupplementRuleService,
)
from app.services.supplement.yaml_impl import YamlSupplementRuleService


def _run(coro):
    """async → sync 桥接"""
    return asyncio.run(coro)


# =============================================================
# Fixture：mock OrderService（不连 MySQL）
# =============================================================
class MockOrderService:
    """支持 .get_order(user_id, order_no) → OrderDTO | None 的最小 mock"""
    def __init__(self, orders: Dict[str, OrderDTO]) -> None:
        self._orders = orders
        self.calls: List[tuple] = []

    async def get_order(self, user_id: int, order_no: str) -> Optional[OrderDTO]:
        self.calls.append((user_id, order_no))
        return self._orders.get(order_no)


def _make_order(
    order_no: str,
    status: str,
    total_amount: float,
    user_id: int = 1001,
) -> OrderDTO:
    now = datetime.datetime(2026, 7, 1, 10, 0, 0)
    return OrderDTO(
        order_no=order_no,
        user_id=user_id,
        status=status,
        items=[],
        total_amount=total_amount,
        create_time=now,
        update_time=now,
    )


# 默认从 config_loader 加载 YAML（spec §2.4 全部走真实 YAML 规则）
def _svc(order_service=None, config=None):
    return YamlSupplementRuleService(order_service=order_service, config=config)


# =============================================================
# #1 运费 · 满 99 包邮（spec §2.4 #1）
# =============================================================
def test_get_shipping_fee_free_shipping_threshold():
    """#1 订单金额 ≥ 99 → 包邮"""
    result = _run(_svc().get_shipping_fee("华东", 1, 99.0, "online"))
    assert result.is_free_shipping is True
    assert result.total_fee == 0.0
    assert result.free_shipping_threshold == 99.0


# =============================================================
# #2 运费 · 偏远地区加价（spec §2.4 #2）
# =============================================================
def test_get_shipping_fee_remote_area_surcharge():
    """#2 地址「西藏」匹配 remote_area_keywords → remote_area_surcharge>0"""
    # 地区名"西藏"匹配 remote_area_keywords → 偏远加价 10 元
    result = _run(_svc().get_shipping_fee("西藏", 1, 50.0, "online"))
    assert result.remote_area_surcharge > 0
    # total_fee = base_fee + remote_area_surcharge（合计 > 单纯 base_fee）
    assert result.total_fee == result.base_fee + result.remote_area_surcharge
    assert result.is_free_shipping is False


# =============================================================
# #3 运费 · 货到付款加价（spec §2.4 #3）
# =============================================================
def test_get_shipping_fee_cod_surcharge():
    """#3 payment_method='cod' → total_fee 比 online 多 5 元（货到付款加价）"""
    online_fee = _run(_svc().get_shipping_fee("华东", 1, 50.0, "online"))
    cod_fee = _run(_svc().get_shipping_fee("华东", 1, 50.0, "cod"))
    assert cod_fee.total_fee > online_fee.total_fee
    assert cod_fee.total_fee - online_fee.total_fee == pytest.approx(5.0)


# =============================================================
# #4 限购 · 普通商品（spec §2.4 #4）
# =============================================================
def test_get_purchase_limit_info_normal_sku():
    """#4 非活动 SKU → limited=True, max_quantity=5（默认）"""
    result = _run(_svc().get_purchase_limit_info("SKU_NORMAL_001", 1001))
    assert result.limited is True
    assert result.max_quantity == 5
    assert result.activity_limited is False


# =============================================================
# #5 限购 · 活动商品（spec §2.4 #5）
# =============================================================
def test_get_purchase_limit_info_activity_sku():
    """#5 活动 SKU → activity_limited=True, max_quantity=1"""
    result = _run(_svc().get_purchase_limit_info("PROMO_2026_D11_001", 1001))
    assert result.activity_limited is True
    assert result.limited is True
    assert result.max_quantity == 1


# =============================================================
# #6 催发货 · 待发货订单（spec §2.4 #6）
# =============================================================
def test_urge_shipment_pending_order():
    """#6 paid 状态订单 → promised_ship_time + urged_count = 0 + tips 非空"""
    order = _make_order("ORD_URGE_001", "paid", 100.0)
    mock = MockOrderService({"ORD_URGE_001": order})
    result = _run(_svc(mock).urge_shipment(1001, "ORD_URGE_001"))
    assert result.order_status == "paid"
    assert result.promised_ship_time is not None
    assert result.urged_count == 0
    assert result.next_urge_available is not None
    assert len(result.tips) >= 1
    # 越权防护
    assert mock.calls == [(1001, "ORD_URGE_001")]


# =============================================================
# #7 延长收货 · shipped + 5 天（spec §2.4 #7）
# =============================================================
def test_extend_receipt_shipped_eligible_5_days():
    """#7 shipped 状态 + extend_days=5 → eligible=True, max=7"""
    order = _make_order("ORD_EXT_001", "shipped", 200.0)
    mock = MockOrderService({"ORD_EXT_001": order})
    result = _run(_svc(mock).extend_receipt(1001, "ORD_EXT_001", 5))
    assert result.eligible is True
    assert result.max_extension_days == 7
    assert result.current_status == "shipped"


# =============================================================
# #8 延长收货 · 已签收状态（spec §2.4 #8）
# =============================================================
def test_extend_receipt_delivered_not_eligible():
    """#8 delivered 状态 → eligible=False, reason 不可延长"""
    order = _make_order("ORD_EXT_002", "delivered", 200.0)
    mock = MockOrderService({"ORD_EXT_002": order})
    result = _run(_svc(mock).extend_receipt(1001, "ORD_EXT_002", 5))
    assert result.eligible is False
    assert "delivered" in result.current_status or "不可延长" in result.reason or "已发货" in result.reason


# =============================================================
# #9 上门取件 · 合法时段（spec §2.4 #9）
# =============================================================
def test_schedule_pickup_valid_slot():
    """#9 time_slot='morning' + 合法地址 → available=True, 3 时段"""
    result = _run(_svc().schedule_pickup("ORD_PK_001", "北京市朝阳区某街道123号", "morning"))
    assert result.available is True
    assert len(result.available_time_slots) == 3
    assert "morning" in result.available_time_slots
    assert "afternoon" in result.available_time_slots
    assert "evening" in result.available_time_slots


# =============================================================
# #10 上门取件 · 非法时段（spec §2.4 #10）
# =============================================================
def test_schedule_pickup_invalid_slot():
    """#10 time_slot='night' → available=False"""
    result = _run(_svc().schedule_pickup("ORD_PK_002", "北京市朝阳区某街道", "night"))
    assert result.available is False
    assert "night" in result.notes[0] or "morning" in " ".join(result.available_time_slots)
