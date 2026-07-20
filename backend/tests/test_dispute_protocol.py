"""
Sprint 20 通用客服中台 · DisputeService Protocol 契约测试（spec §3.4 · 6 用例）

策略：
- 质量问题鉴定：mock OrderService 返不同金额 Order，验证举证责任判定
- 平台介入：mock OrderService，按 dispute_type 列要求
- 举报假货：纯 YAML，不调 OrderService

env 兜底由 conftest.py 提供。
"""
import asyncio
import datetime
from typing import Any, Dict, List, Optional

import pytest

from app.schemas.business import Order as OrderDTO
from app.services.dispute.protocols import (
    DisputeError,
    DisputeService,
)
from app.services.dispute.yaml_impl import YamlDisputeService


def _run(coro):
    """async → sync 桥接"""
    return asyncio.run(coro)


class MockOrderService:
    def __init__(self, orders: Dict[str, OrderDTO]) -> None:
        self._orders = orders
        self.calls: List[tuple] = []

    async def get_order(self, user_id: int, order_no: str) -> Optional[OrderDTO]:
        self.calls.append((user_id, order_no))
        return self._orders.get(order_no)


def _make_order(order_no: str, status: str, total_amount: float) -> OrderDTO:
    now = datetime.datetime(2026, 7, 1, 10, 0, 0)
    return OrderDTO(
        order_no=order_no,
        user_id=1001,
        status=status,
        items=[],
        total_amount=total_amount,
        create_time=now,
        update_time=now,
    )


def _svc(order_service=None, config=None):
    return YamlDisputeService(order_service=order_service, config=config)


# =============================================================
# #1 质量问题鉴定 · 高价值订单（spec §3.4 #1）
# =============================================================
def test_quality_dispute_high_value_seller_burden():
    """#1 订单金额 > 1000 → burden_of_proof='seller'"""
    order = _make_order("ORD_Q1", "delivered", 1500.0)
    mock = MockOrderService({"ORD_Q1": order})
    result = _run(_svc(mock).get_quality_dispute_process(1001, "ORD_Q1"))
    assert result.burden_of_proof == "seller"
    assert len(result.process_steps) >= 1
    assert len(result.appeal_channels) >= 1
    assert result.evidence_deadline_hours == 48


# =============================================================
# #2 质量问题鉴定 · 低价值订单（spec §3.4 #2）
# =============================================================
def test_quality_dispute_low_value_buyer_burden():
    """#2 订单金额 ≤ 1000 → burden_of_proof='buyer'"""
    order = _make_order("ORD_Q2", "delivered", 200.0)
    mock = MockOrderService({"ORD_Q2": order})
    result = _run(_svc(mock).get_quality_dispute_process(1001, "ORD_Q2"))
    assert result.burden_of_proof == "buyer"
    assert result.evidence_deadline_hours == 48
    # buyer 举证：含买家侧的证据（问题照片）
    assert len(result.evidence_required) >= 1


# =============================================================
# #3 平台介入 · 满足条件（spec §3.4 #3）
# =============================================================
def test_platform_intervene_refund_rejected_eligible():
    """#3 dispute_type='refund_rejected' → eligible=True"""
    order = _make_order("ORD_PI1", "refund_rejected", 500.0)
    mock = MockOrderService({"ORD_PI1": order})
    result = _run(_svc(mock).check_platform_intervene_eligibility(1001, "ORD_PI1", "refund_rejected"))
    assert result.eligible is True
    assert result.dispute_type == "refund_rejected"
    assert len(result.required_conditions) >= 1
    # 后果列表非空
    assert len(result.consequences) >= 1


# =============================================================
# #4 平台介入 · 不支持的纠纷类型（spec §3.4 #4）
# =============================================================
def test_platform_intervene_unsupported_type():
    """#4 未知 dispute_type → eligible=False"""
    order = _make_order("ORD_PI2", "refund_rejected", 500.0)
    mock = MockOrderService({"ORD_PI2": order})
    result = _run(_svc(mock).check_platform_intervene_eligibility(1001, "ORD_PI2", "unknown_type"))
    assert result.eligible is False
    assert "unknown_type" in result.reason


# =============================================================
# #5 举报假货 · 完整流程（spec §3.4 #5）
# =============================================================
def test_report_fake_goods_full_process():
    """#5 完整流程 → channels + evidence + penalties 全部非空"""
    result = _run(_svc().get_report_fake_goods_process("ORD_FAKE_001"))
    assert result.order_no == "ORD_FAKE_001"
    assert len(result.report_channels) >= 1
    assert len(result.evidence_required) >= 1
    assert len(result.possible_penalties) >= 1
    assert len(result.notes) >= 1


# =============================================================
# #6 举报假货 · 处理时限（spec §3.4 #6）
# =============================================================
def test_report_fake_goods_processing_days():
    """#6 processing_days=7（YAML 定义）"""
    result = _run(_svc().get_report_fake_goods_process("ORD_FAKE_002"))
    assert result.processing_days == 7
