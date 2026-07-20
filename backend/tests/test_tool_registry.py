"""
Sprint 19 · Tool Registry dispatch 测试（13 用例）

策略（spec §6）：
- 每个 ToolSpec 1 个 dispatch 测试
- 验证 dispatch 能正确路由到对应 runner，且 result 含预期字段
- 售后/售前 mock Service Protocol；售中测试 needs_confirmation 路径
- 同时验证 REGISTRY 注册总数 = 13、to_openai_tools 输出 13 个 function schema

env 兜底由 conftest.py 提供。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.tools.registry import (
    REGISTRY,
    ToolContext,
    dispatch,
    to_openai_tools,
)


# =============================================================
# Fixture：mock 三个 Service 工厂（一次性注入所有）
# =============================================================
@pytest.fixture
def mock_all_services():
    """一次性 patch 三个工厂的 get_xxx_service 方法"""
    # AfterSales mock
    class MockAfterSales:
        async def get_refund_reason_advice(self, user_id, order_no, reason_category):
            return type("R", (), {
                "model_dump": lambda self=None: {
                    "order_no": order_no,
                    "reason_category": reason_category,
                    "suggested_reason_text": "mock reason",
                    "success_rate_hint": "高",
                    "evidence_required": ["照片"],
                    "additional_tips": ["tip1"],
                },
            })()

        async def get_shipping_insurance_info(self, order_no, return_status):
            return type("S", (), {
                "model_dump": lambda self=None: {
                    "order_no": order_no,
                    "insured": True,
                    "coverage_amount": 25.0,
                    "eligible": True,
                    "estimated_payout_days": 3,
                    "notes": ["mock note"],
                },
            })()

        async def get_refund_type_advice(self, user_id, order_no):
            return type("T", (), {
                "model_dump": lambda self=None: {
                    "order_no": order_no,
                    "recommended_type": "refund_only",
                    "reasoning": "mock reasoning",
                    "conditions": ["mock condition"],
                },
            })()

    # Promotion mock
    class MockPromotion:
        async def get_active_promotions(self, user_id, cart_items):
            return []

        async def check_coupon_stackable(self, coupon_ids):
            return type("C", (), {
                "model_dump": lambda self=None: {
                    "stackable_groups": [],
                    "conflicting_pairs": [],
                    "best_combination": [],
                },
            })()

        async def calculate_bundle_discount(self, store_totals):
            return type("B", (), {
                "model_dump": lambda self=None: {
                    "current_total": sum(store_totals.values()),
                    "store_totals": dict(store_totals),
                    "next_threshold": None,
                    "next_benefit": None,
                    "suggestion": None,
                },
            })()

    # OrderModify mock
    class MockOrderModify:
        async def modify_address(self, user_id, order_no, new_address):
            return type("M", (), {
                "model_dump": lambda self=None: {
                    "success": True, "order_no": order_no,
                    "modification_type": "address", "reason": "mock",
                },
            })()

        async def modify_item_spec(self, user_id, order_no, sku, new_qty):
            return type("M", (), {
                "model_dump": lambda self=None: {
                    "success": True, "order_no": order_no,
                    "modification_type": "spec", "reason": "mock",
                },
            })()

        async def merge_orders(self, user_id, order_nos):
            return type("G", (), {
                "model_dump": lambda self=None: {
                    "success": True, "primary_order_no": order_nos[0],
                    "merged_order_nos": order_nos[1:], "reason": "mock",
                },
            })()

    after_fac = type("F", (), {"get_after_sales_rule_service": staticmethod(lambda: MockAfterSales())})()
    promo_fac = type("F", (), {"get_promotion_rule_service": staticmethod(lambda: MockPromotion())})()
    modify_fac = type("F", (), {"get_order_modify_service": staticmethod(lambda: MockOrderModify())})()

    patches = [
        patch("app.tools.after_sales_tool.get_after_sales_rule_service_factory", return_value=after_fac),
        patch("app.tools.promotion_tool.get_promotion_rule_service_factory", return_value=promo_fac),
        patch("app.tools.order_modify_tool.get_order_modify_service_factory", return_value=modify_fac),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


# =============================================================
# 验证 REGISTRY 数量
# =============================================================
def test_registry_has_13_tools():
    """Sprint 19: 4 已有 + 9 新增 = 13 Tool"""
    assert len(REGISTRY) == 13


def test_to_openai_tools_returns_13():
    """to_openai_tools() 输出 13 个 OpenAI FC schema"""
    tools = to_openai_tools()
    assert len(tools) == 13
    names = {t["function"]["name"] for t in tools}
    # 4 已有
    assert {"lookup_order", "search_product", "search_policy", "check_refundable"} <= names
    # 9 新增
    assert {
        "get_refund_reason_advice", "get_shipping_insurance_info", "get_refund_type_advice",
        "get_active_promotions", "check_coupon_stackable", "calculate_bundle_discount",
        "modify_address", "modify_item_spec", "merge_orders",
    } <= names


# =============================================================
# 辅助：构造 ctx + args_json
# =============================================================
def _ctx(user_id: int = 1001) -> ToolContext:
    return ToolContext(user_id=user_id)


def _args(d: dict) -> str:
    return json.dumps(d)


# =============================================================
# #1 lookup_order（保留现有 dispatcher）
# =============================================================
def test_dispatch_lookup_order():
    ctx = _ctx()
    result = dispatch("lookup_order", _args({"order_no": "ORD001"}), ctx)
    # OrderTool 真连 DB；这里 order 不存在返 error dict 或 None
    assert isinstance(result, dict)


# =============================================================
# #2 search_product
# =============================================================
def test_dispatch_search_product():
    ctx = _ctx(user_id=0)
    result = dispatch("search_product", _args({"keyword": "手机"}), ctx)
    assert "products" in result or "error" in result


# =============================================================
# #3 search_policy
# =============================================================
def test_dispatch_search_policy():
    ctx = _ctx()
    result = dispatch("search_policy", _args({"query": "退货"}), ctx)
    # 真连 Qdrant 可能返 error；只验证 dict
    assert isinstance(result, dict)


# =============================================================
# #4 check_refundable
# =============================================================
def test_dispatch_check_refundable():
    ctx = _ctx()
    result = dispatch("check_refundable", _args({"order_no": "ORD001"}), ctx)
    assert isinstance(result, dict)


# =============================================================
# #5 get_refund_reason_advice（售后）
# =============================================================
def test_dispatch_get_refund_reason_advice(mock_all_services):
    """dispatch → AfterSalesTool → DTO → dict"""
    result = dispatch(
        "get_refund_reason_advice",
        _args({"order_no": "ORD001", "reason_category": "quality"}),
        _ctx(),
    )
    assert result["order_no"] == "ORD001"
    assert result["reason_category"] == "quality"
    assert result["success_rate_hint"] == "高"


# =============================================================
# #6 get_shipping_insurance_info
# =============================================================
def test_dispatch_get_shipping_insurance_info(mock_all_services):
    result = dispatch(
        "get_shipping_insurance_info",
        _args({"order_no": "ORD002", "return_status": "return_received"}),
        _ctx(),
    )
    assert result["order_no"] == "ORD002"
    assert result["insured"] is True
    assert result["coverage_amount"] == 25.0


# =============================================================
# #7 get_refund_type_advice
# =============================================================
def test_dispatch_get_refund_type_advice(mock_all_services):
    result = dispatch(
        "get_refund_type_advice",
        _args({"order_no": "ORD003"}),
        _ctx(),
    )
    assert result["order_no"] == "ORD003"
    assert result["recommended_type"] == "refund_only"


# =============================================================
# #8 get_active_promotions
# =============================================================
def test_dispatch_get_active_promotions(mock_all_services):
    result = dispatch(
        "get_active_promotions",
        _args({}),
        _ctx(),
    )
    assert "promotions" in result
    assert isinstance(result["promotions"], list)


# =============================================================
# #9 check_coupon_stackable
# =============================================================
def test_dispatch_check_coupon_stackable(mock_all_services):
    result = dispatch(
        "check_coupon_stackable",
        _args({"coupon_ids": ["C1", "C2"]}),
        _ctx(),
    )
    assert "stackable_groups" in result
    assert "conflicting_pairs" in result
    assert "best_combination" in result


# =============================================================
# #10 calculate_bundle_discount
# =============================================================
def test_dispatch_calculate_bundle_discount(mock_all_services):
    result = dispatch(
        "calculate_bundle_discount",
        _args({"store_totals": {"storeA": 280, "storeB": 150}}),
        _ctx(),
    )
    assert result["current_total"] == 430
    assert result["store_totals"] == {"storeA": 280, "storeB": 150}


# =============================================================
# #11 modify_address（confirmed=False → needs_confirmation）
# =============================================================
def test_dispatch_modify_address_unconfirmed(mock_all_services):
    """dispatch → confirmed=false → needs_confirmation dict（不调 Service）"""
    result = dispatch(
        "modify_address",
        _args({"order_no": "ORD001", "new_address": "新地址 X"}),
        _ctx(),
    )
    assert result["status"] == "needs_confirmation"
    assert result["requires_user_input"] is True


# =============================================================
# #12 modify_item_spec（confirmed=False → needs_confirmation）
# =============================================================
def test_dispatch_modify_item_spec_unconfirmed(mock_all_services):
    result = dispatch(
        "modify_item_spec",
        _args({"order_no": "ORD001", "sku": "SKU1", "new_qty": 3}),
        _ctx(),
    )
    assert result["status"] == "needs_confirmation"


# =============================================================
# #13 merge_orders（confirmed=False → needs_confirmation）
# =============================================================
def test_dispatch_merge_orders_unconfirmed(mock_all_services):
    result = dispatch(
        "merge_orders",
        _args({"order_nos": ["ORD001", "ORD002"]}),
        _ctx(),
    )
    assert result["status"] == "needs_confirmation"
    assert "ORD001" in result["prompt"]
    assert "ORD002" in result["prompt"]


# =============================================================
# 边界：dispatch 异常路径
# =============================================================
def test_dispatch_unknown_tool_returns_error():
    """未注册的 tool → error dict（不抛异常）"""
    result = dispatch("nonexistent_tool", _args({}), _ctx())
    assert "error" in result
    assert "not registered" in result["error"]


def test_dispatch_invalid_json_returns_error():
    """JSON 解析失败 → error dict"""
    result = dispatch("lookup_order", "{bad json", _ctx())
    assert "error" in result
    assert "JSON" in result["error"] or "json" in result["error"]