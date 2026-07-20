"""
Sprint 19 · AfterSalesTool 测试（6 用例）

策略（spec §6）：
- mock Service Protocol（不连真实 YAML/MySQL）；注入到工厂
- 验证 Tool 类纯委托 + DTO→dict 适配 + 异常路径返 error dict
- 不验证 Service 内部逻辑（已有 test_after_sales_protocol.py 覆盖）

env 兜底由 conftest.py 提供（JWT_SECRET / DATABASE_URL）。
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from app.schemas.business import (
    RefundReasonAdvice,
    RefundTypeAdvice,
    ShippingInsuranceInfo,
)
from app.tools.after_sales_tool import AfterSalesTool


# =============================================================
# Mock Service（实现 Protocol · 不连 YAML/MySQL）
# =============================================================
class MockAfterSalesService:
    """完整实现 AfterSalesRuleService Protocol；测试方法自己控制返回。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def get_refund_reason_advice(
        self, user_id: int, order_no: str, reason_category: str,
    ) -> RefundReasonAdvice:
        self.calls.append(("get_refund_reason_advice", user_id, order_no, reason_category))
        return RefundReasonAdvice(
            order_no=order_no,
            reason_category=reason_category,
            suggested_reason_text=f"建议理由：{reason_category}",
            success_rate_hint="高",
            evidence_required=["照片"],
            additional_tips=["补充说明"],
        )

    async def get_shipping_insurance_info(
        self, order_no: str, return_status: str,
    ) -> ShippingInsuranceInfo:
        self.calls.append(("get_shipping_insurance_info", order_no, return_status))
        return ShippingInsuranceInfo(
            order_no=order_no,
            insured=True,
            coverage_amount=25.0,
            eligible=True,
            estimated_payout_days=3,
            notes=["运费险规则"],
        )

    async def get_refund_type_advice(
        self, user_id: int, order_no: str,
    ) -> RefundTypeAdvice:
        self.calls.append(("get_refund_type_advice", user_id, order_no))
        return RefundTypeAdvice(
            order_no=order_no,
            recommended_type="refund_only",
            reasoning="订单已发货，建议仅退款",
            conditions=["金额 < 200", "已发货 < 7 天"],
        )


# =============================================================
# Fixture：注入 mock factory（不连真实工厂单例）
# =============================================================
@pytest.fixture
def mock_svc():
    """返回 Mock Service 实例，并 patch 工厂方法返回它。"""
    svc = MockAfterSalesService()
    factory_inst = type("F", (), {"get_after_sales_rule_service": staticmethod(lambda: svc)})()
    with patch(
        "app.tools.after_sales_tool.get_after_sales_rule_service_factory",
        return_value=factory_inst,
    ):
        yield svc


# =============================================================
# #1 get_refund_reason_advice（正常路径）
# =============================================================
def test_get_refund_reason_advice_calls_service_and_dumps_dict(mock_svc):
    """#1 调 Service → DTO.model_dump() → dict"""
    result = AfterSalesTool.get_refund_reason_advice(1001, "ORD001", "quality")
    assert isinstance(result, dict)
    assert result["order_no"] == "ORD001"
    assert result["reason_category"] == "quality"
    assert result["success_rate_hint"] == "高"
    assert "照片" in result["evidence_required"]
    # 验证 mock 被调（user_id/order_no/reason_category 都正确传递）
    assert mock_svc.calls[-1] == ("get_refund_reason_advice", 1001, "ORD001", "quality")


# =============================================================
# #2 get_refund_reason_advice（异常 → error dict）
# =============================================================
def test_get_refund_reason_advice_returns_error_dict_on_exception(mock_svc):
    """#2 Service 抛异常 → Tool 吞掉 → 返 {"error": "..."} dict"""
    async def boom(*args, **kwargs):
        raise RuntimeError("simulated service failure")
    mock_svc.get_refund_reason_advice = boom  # type: ignore[assignment]
    result = AfterSalesTool.get_refund_reason_advice(1001, "ORD001", "quality")
    assert isinstance(result, dict)
    assert "error" in result
    assert "RuntimeError" in result["error"]
    assert "simulated service failure" in result["error"]


# =============================================================
# #3 get_shipping_insurance_info（正常）
# =============================================================
def test_get_shipping_insurance_info_returns_dict(mock_svc):
    """#3 运费险：insured=True + coverage=25.0"""
    result = AfterSalesTool.get_shipping_insurance_info("ORD002", "return_received")
    assert result["order_no"] == "ORD002"
    assert result["insured"] is True
    assert result["coverage_amount"] == 25.0
    assert result["estimated_payout_days"] == 3


# =============================================================
# #4 get_shipping_insurance_info（异常）
# =============================================================
def test_get_shipping_insurance_info_returns_error_dict_on_exception(mock_svc):
    """#4 异常路径返 error dict（不阻断 Agent）"""
    async def boom(*args, **kwargs):
        raise ValueError("bad return_status")
    mock_svc.get_shipping_insurance_info = boom  # type: ignore[assignment]
    result = AfterSalesTool.get_shipping_insurance_info("ORD003", "refunded")
    assert "error" in result
    assert "ValueError" in result["error"]


# =============================================================
# #5 get_refund_type_advice（正常）
# =============================================================
def test_get_refund_type_advice_returns_dict(mock_svc):
    """#5 退款类型建议 → dict"""
    result = AfterSalesTool.get_refund_type_advice(1001, "ORD005")
    assert result["order_no"] == "ORD005"
    assert result["recommended_type"] == "refund_only"
    assert len(result["conditions"]) >= 1


# =============================================================
# #6 get_refund_type_advice（None 返回值 → error dict）
# =============================================================
def test_get_refund_type_advice_none_returns_error_dict(mock_svc):
    """#6 Service 返 None（订单不属于该 user 等）→ Tool 返 error dict"""
    async def returns_none(*args, **kwargs):
        return None
    mock_svc.get_refund_type_advice = returns_none  # type: ignore[assignment]
    result = AfterSalesTool.get_refund_type_advice(1001, "ORD_OTHER")
    assert "error" in result
    assert "no advice" in result["error"]


# =============================================================
# 模块隔离验证（CLAUDE.md §9.2.2）：Tool 不直接 import models.*
# =============================================================
def test_after_sales_tool_no_models_import():
    """Tool 类禁止直接 import app.models.* —— 必须走 Service Protocol"""
    import inspect
    from app.tools import after_sales_tool as mod

    source = inspect.getsource(mod)
    assert "from app.models" not in source
    assert "import app.models" not in source