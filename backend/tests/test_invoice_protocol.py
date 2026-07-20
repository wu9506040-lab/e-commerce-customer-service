"""
Sprint 20 通用客服中台 · InvoiceService Protocol 契约测试（spec §4.4 · 4 用例）

策略：
- 全部用例 mock OrderService 返不同金额 Order，验证满额 + 类型判定逻辑。

env 兜底由 conftest.py 提供。
"""
import asyncio
import datetime
from typing import Any, Dict, List, Optional

import pytest

from app.schemas.business import Order as OrderDTO
from app.services.invoice.protocols import (
    InvoiceService,
)
from app.services.invoice.yaml_impl import YamlInvoiceService


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


def _make_order(order_no: str, total_amount: float, status: str = "completed") -> OrderDTO:
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
    return YamlInvoiceService(order_service=order_service, config=config)


# =============================================================
# #1 个人电子发票 · 满 50 元（spec §4.4 #1）
# =============================================================
def test_invoice_personal_electronic_eligible():
    """#1 personal_electronic + 订单金额 ≥ 50 → eligible=True, tax_id_required=False"""
    order = _make_order("ORD_INV_001", 100.0)
    mock = MockOrderService({"ORD_INV_001": order})
    result = _run(_svc(mock).check_invoice_eligibility(1001, "ORD_INV_001", "personal_electronic"))
    assert result.eligible is True
    assert result.invoice_type == "personal_electronic"
    assert result.tax_id_required is False
    assert result.amount_threshold_met is True
    assert result.eligible_amount == 100.0


# =============================================================
# #2 个人电子发票 · 不足 50 元（spec §4.4 #2）
# =============================================================
def test_invoice_personal_electronic_below_threshold():
    """#2 personal_electronic + 订单金额 < 50 → eligible=False, amount_threshold_met=False"""
    order = _make_order("ORD_INV_002", 30.0)
    mock = MockOrderService({"ORD_INV_002": order})
    result = _run(_svc(mock).check_invoice_eligibility(1001, "ORD_INV_002", "personal_electronic"))
    assert result.eligible is False
    assert result.amount_threshold_met is False
    assert result.eligible_amount == 0.0
    assert result.current_order_amount == 30.0
    assert result.minimum_amount == 50.0


# =============================================================
# #3 公司专票 · 满 50 元（spec §4.4 #3）
# =============================================================
def test_invoice_company_special_eligible_tax_id():
    """#3 company_special + 订单金额 ≥ 50 → eligible=True, tax_id_required=True, title_required=True"""
    order = _make_order("ORD_INV_003", 500.0)
    mock = MockOrderService({"ORD_INV_003": order})
    result = _run(_svc(mock).check_invoice_eligibility(1001, "ORD_INV_003", "company_special"))
    assert result.eligible is True
    assert result.tax_id_required is True
    assert result.invoice_title_required is True
    assert result.amount_threshold_met is True
    # 公司专票：notes 应包含"公司资质证明"
    notes_text = " ".join(result.notes)
    assert "公司资质证明" in notes_text or "资质" in notes_text


# =============================================================
# #4 申请 URL 完整（spec §4.4 #4）
# =============================================================
def test_invoice_application_url_present():
    """#4 application_url 非空 + 含 order_no 标识"""
    order = _make_order("ORD_INV_004", 200.0)
    mock = MockOrderService({"ORD_INV_004": order})
    result = _run(_svc(mock).check_invoice_eligibility(1001, "ORD_INV_004", "personal_electronic"))
    assert result.application_url
    assert isinstance(result.application_url, str)
    assert len(result.application_url) > 10
    # 含订单号或参数
    assert "ORD_INV_004" in result.application_url or "order_no" in result.application_url
