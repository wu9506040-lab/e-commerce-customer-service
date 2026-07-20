"""
Sprint 19 · OrderModifyTool 测试（6 用例 · 重点验证写操作 2 步确认）

策略（spec §6）：
- 3 个写操作各 2 用例：confirmed=False → needs_confirmation dict；
  confirmed=True → 调 Service → ModifyResult/MergeResult.model_dump()
- 1 个模块隔离验证

env 兜底由 conftest.py 提供。
"""
from __future__ import annotations

import datetime
from typing import Any, List, Optional
from unittest.mock import patch

import pytest

from app.schemas.business import MergeResult, ModifyResult
from app.tools.order_modify_tool import OrderModifyTool, _check_confirmation


# =============================================================
# Mock Service（实现 OrderModifyService Protocol）
# =============================================================
class MockOrderModifyService:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def modify_address(
        self, user_id: int, order_no: str, new_address: str,
    ) -> ModifyResult:
        self.calls.append(("modify_address", user_id, order_no, new_address))
        return ModifyResult(
            success=True,
            order_no=order_no,
            modification_type="address",
            reason="地址修改成功",
            before_snapshot={"shipping_address": "旧地址"},
            after_snapshot={"shipping_address": new_address},
        )

    async def modify_item_spec(
        self, user_id: int, order_no: str, sku: str, new_qty: Optional[int] = None,
    ) -> ModifyResult:
        self.calls.append(("modify_item_spec", user_id, order_no, sku, new_qty))
        return ModifyResult(
            success=True,
            order_no=order_no,
            modification_type="spec",
            reason=f"已将 {sku} 数量改为 {new_qty}",
            before_snapshot={"sku": sku, "qty": 1},
            after_snapshot={"sku": sku, "qty": new_qty},
        )

    async def merge_orders(
        self, user_id: int, order_nos: List[str],
    ) -> MergeResult:
        self.calls.append(("merge_orders", user_id, list(order_nos)))
        return MergeResult(
            success=True,
            primary_order_no=order_nos[0],
            merged_order_nos=order_nos[1:],
            reason=f"已合并 {len(order_nos)} 个订单",
        )


@pytest.fixture
def mock_svc():
    svc = MockOrderModifyService()
    factory_inst = type("F", (), {"get_order_modify_service": staticmethod(lambda: svc)})()
    with patch(
        "app.tools.order_modify_tool.get_order_modify_service_factory",
        return_value=factory_inst,
    ):
        yield svc


# =============================================================
# #1 modify_address · confirmed=False → needs_confirmation
# =============================================================
def test_modify_address_unconfirmed_returns_needs_confirmation(mock_svc):
    """#1 默认 confirmed=False → 返 needs_confirmation dict（不调 Service）"""
    result = OrderModifyTool.modify_address(1001, "ORD001", "新地址 X")
    assert result["status"] == "needs_confirmation"
    assert result["requires_user_input"] is True
    assert "ORD001" in result["prompt"]
    assert "新地址 X" in result["prompt"]
    # 关键：未确认时不调 Service
    assert len(mock_svc.calls) == 0


# =============================================================
# #2 modify_address · confirmed=True → 调 Service 写
# =============================================================
def test_modify_address_confirmed_executes_service(mock_svc):
    """#2 confirmed=True → 调 Service → ModifyResult.model_dump()"""
    result = OrderModifyTool.modify_address(1001, "ORD001", "新地址 X", confirmed=True)
    assert result["success"] is True
    assert result["order_no"] == "ORD001"
    assert result["modification_type"] == "address"
    assert result["after_snapshot"]["shipping_address"] == "新地址 X"
    # 验证 mock 收到正确参数
    assert mock_svc.calls[-1] == ("modify_address", 1001, "ORD001", "新地址 X")


# =============================================================
# #3 modify_item_spec · confirmed=False
# =============================================================
def test_modify_item_spec_unconfirmed_returns_needs_confirmation(mock_svc):
    """#3 未确认 → needs_confirmation（不调 Service）"""
    result = OrderModifyTool.modify_item_spec(1001, "ORD001", "SKU1", new_qty=3)
    assert result["status"] == "needs_confirmation"
    assert "SKU1" in result["prompt"]
    assert "3" in result["prompt"]
    assert len(mock_svc.calls) == 0


# =============================================================
# #4 modify_item_spec · confirmed=True
# =============================================================
def test_modify_item_spec_confirmed_executes_service(mock_svc):
    """#4 confirmed=True → 调 Service → ModifyResult.model_dump()"""
    result = OrderModifyTool.modify_item_spec(1001, "ORD001", "SKU1", new_qty=3, confirmed=True)
    assert result["success"] is True
    assert result["modification_type"] == "spec"
    assert result["after_snapshot"]["qty"] == 3
    assert mock_svc.calls[-1] == ("modify_item_spec", 1001, "ORD001", "SKU1", 3)


# =============================================================
# #5 merge_orders · confirmed=False → needs_confirmation
# =============================================================
def test_merge_orders_unconfirmed_returns_needs_confirmation(mock_svc):
    """#5 未确认 → needs_confirmation（不调 Service）"""
    result = OrderModifyTool.merge_orders(1001, ["ORD001", "ORD002", "ORD003"])
    assert result["status"] == "needs_confirmation"
    assert "ORD001" in result["prompt"]
    assert "ORD002" in result["prompt"]
    assert "ORD003" in result["prompt"]
    assert "合并" in result["prompt"]
    assert len(mock_svc.calls) == 0


# =============================================================
# #6 merge_orders · confirmed=True → 调 Service
# =============================================================
def test_merge_orders_confirmed_executes_service(mock_svc):
    """#6 confirmed=True → 调 Service → MergeResult.model_dump()"""
    result = OrderModifyTool.merge_orders(1001, ["ORD001", "ORD002"], confirmed=True)
    assert result["success"] is True
    assert result["primary_order_no"] == "ORD001"
    assert result["merged_order_nos"] == ["ORD002"]
    assert "合并" in result["reason"]
    assert mock_svc.calls[-1] == ("merge_orders", 1001, ["ORD001", "ORD002"])


# =============================================================
# 额外：_check_confirmation helper 单测（覆盖纯函数）
# =============================================================
def test_check_confirmation_helper():
    """_check_confirmation helper 单测：False → dict；True → None"""
    assert _check_confirmation(False, "测试动作") == {
        "status": "needs_confirmation",
        "prompt": "即将测试动作，请用户回复「确认」后再执行。",
        "requires_user_input": True,
    }
    assert _check_confirmation(True, "测试动作") is None


# =============================================================
# 模块隔离验证
# =============================================================
def test_order_modify_tool_no_models_import():
    """Tool 类禁止直接 import app.models.*"""
    import inspect
    from app.tools import order_modify_tool as mod

    source = inspect.getsource(mod)
    assert "from app.models" not in source
    assert "import app.models" not in source