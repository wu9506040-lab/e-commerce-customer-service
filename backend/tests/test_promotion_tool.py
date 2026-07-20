"""
Sprint 19 · PromotionTool 测试（5 用例）

策略：mock Service Protocol，验证 Tool 类纯委托 + DTO→dict 适配。

env 兜底由 conftest.py 提供。
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from app.schemas.business import (
    BundleDiscountResult,
    CouponStackResult,
    Promotion,
)
from app.tools.promotion_tool import PromotionTool


# =============================================================
# Mock Service
# =============================================================
class MockPromotionService:
    """完整实现 PromotionRuleService Protocol"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def get_active_promotions(
        self, user_id: int, cart_items: List[dict],
    ) -> List[Promotion]:
        self.calls.append(("get_active_promotions", user_id, len(cart_items)))
        now = datetime.datetime(2026, 7, 1, 12, 0, 0)
        return [
            Promotion(
                promotion_id="P1", name="满200减30", type="full_reduction",
                threshold=200.0, benefit=30.0, applicable_stores=[],
                applicable_categories=["美妆"], start_time=now, end_time=now,
                stackable=True,
            ),
        ]

    async def check_coupon_stackable(
        self, coupon_ids: List[str],
    ) -> CouponStackResult:
        self.calls.append(("check_coupon_stackable", list(coupon_ids)))
        return CouponStackResult(
            stackable_groups=[["C1"], ["C2"]],
            conflicting_pairs=[{"a": "C1", "b": "C2", "reason": "同类型互斥"}],
            best_combination=["C1"],
        )

    async def calculate_bundle_discount(
        self, store_totals: Dict[str, float],
    ) -> BundleDiscountResult:
        self.calls.append(("calculate_bundle_discount", dict(store_totals)))
        total = sum(store_totals.values())
        return BundleDiscountResult(
            current_total=total,
            store_totals=dict(store_totals),
            next_threshold=300.0,
            next_benefit=50.0,
            suggestion=f"再加 {300.0 - total} 元可减 50",
        )


@pytest.fixture
def mock_svc():
    svc = MockPromotionService()
    factory_inst = type("F", (), {"get_promotion_rule_service": staticmethod(lambda: svc)})()
    with patch(
        "app.tools.promotion_tool.get_promotion_rule_service_factory",
        return_value=factory_inst,
    ):
        yield svc


# =============================================================
# #1 get_active_promotions
# =============================================================
def test_get_active_promotions_returns_list_dict(mock_svc):
    """#1 返回 {"promotions": [dict, ...]} 格式"""
    result = PromotionTool.get_active_promotions(1001, [])
    assert "promotions" in result
    assert isinstance(result["promotions"], list)
    assert len(result["promotions"]) == 1
    assert result["promotions"][0]["promotion_id"] == "P1"
    assert result["promotions"][0]["type"] == "full_reduction"


# =============================================================
# #2 check_coupon_stackable
# =============================================================
def test_check_coupon_stackable_returns_dict(mock_svc):
    """#2 券叠加校验返回完整 dict"""
    result = PromotionTool.check_coupon_stackable(["C1", "C2"])
    assert "stackable_groups" in result
    assert "conflicting_pairs" in result
    assert "best_combination" in result
    assert len(result["conflicting_pairs"]) == 1
    assert result["best_combination"] == ["C1"]
    # 验证 mock 收到完整 coupon_ids
    assert mock_svc.calls[-1] == ("check_coupon_stackable", ["C1", "C2"])


# =============================================================
# #3 calculate_bundle_discount
# =============================================================
def test_calculate_bundle_discount_returns_dict(mock_svc):
    """#3 跨店满减计算 → dict"""
    result = PromotionTool.calculate_bundle_discount({"storeA": 280, "storeB": 150})
    assert result["current_total"] == 430
    assert result["store_totals"] == {"storeA": 280, "storeB": 150}
    assert result["next_threshold"] == 300.0
    assert result["next_benefit"] == 50.0
    assert "30" in result["suggestion"] or "50" in result["suggestion"]


# =============================================================
# #4 异常路径：所有方法返 error dict
# =============================================================
def test_exception_returns_error_dict(mock_svc):
    """#4 Service 抛异常 → Tool 返 error dict"""
    async def boom(*args, **kwargs):
        raise RuntimeError("promo svc down")
    mock_svc.get_active_promotions = boom  # type: ignore[assignment]
    result = PromotionTool.get_active_promotions(1001, [])
    assert "error" in result
    assert "RuntimeError" in result["error"]


# =============================================================
# #5 模块隔离验证
# =============================================================
def test_promotion_tool_no_models_import():
    """Tool 类禁止直接 import app.models.*"""
    import inspect
    from app.tools import promotion_tool as mod

    source = inspect.getsource(mod)
    assert "from app.models" not in source
    assert "import app.models" not in source