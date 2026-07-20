"""
Sprint 16 · RefundService Protocol 契约测试（spec §1.6 · 8 用例）

策略：
- #1-#6 MySQL 默认实现：patch mysql_impl.with_safe_session → 真 SQLite session（seed 数据），
  验证 ORM→DTO 映射 / 越权过滤 / 分页 cursor / 状态过滤。
- #7-#8 Tool 改用 Protocol：patch 工厂返回 mock Protocol，验证 Tool 委托而非直连 DB。

env 兜底由 conftest.py 提供（JWT_SECRET / DATABASE_URL）。
"""
import asyncio
import datetime
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.order import Order as OrderORM
from app.models.refund import Refund as RefundORM, RefundStatus
from app.services.refund import mysql_impl
from app.services.refund.mysql_impl import MySQLRefundService
from app.services.refund.protocols import RefundService


# =============================================================
# 真 SQLite session + seed（供 #1-#6）
# =============================================================
@pytest.fixture
def seeded_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    now = datetime.datetime(2026, 7, 1, 10, 0, 0)
    # 订单（refunds 关联）
    orders = [
        OrderORM(id=1, order_no="ORD001", user_id=1001, status="paid",
                 total_amount=100, create_time=now, update_time=now),
        OrderORM(id=2, order_no="ORD002", user_id=1001, status="shipped",
                 total_amount=200, create_time=now + datetime.timedelta(days=1),
                 update_time=now + datetime.timedelta(days=1)),
        OrderORM(id=3, order_no="ORD003", user_id=1001, status="completed",
                 total_amount=300, create_time=now + datetime.timedelta(days=2),
                 update_time=now + datetime.timedelta(days=2)),
        OrderORM(id=9, order_no="ORD_OTHER", user_id=2002, status="paid",
                 total_amount=50, create_time=now, update_time=now),
    ]
    # 退款（用户 1001 三笔，不同状态；用户 2002 一笔，越权用）
    refunds = [
        RefundORM(id=1, refund_no="RF001", order_id=1, user_id=1001,
                  reason="不喜欢", status=RefundStatus.PENDING.value,
                  amount=100, create_time=now, update_time=now),
        RefundORM(id=2, refund_no="RF002", order_id=2, user_id=1001,
                  reason="质量问题", status=RefundStatus.APPROVED.value,
                  amount=200, create_time=now + datetime.timedelta(days=1),
                  update_time=now + datetime.timedelta(days=1)),
        RefundORM(id=3, refund_no="RF003", order_id=3, user_id=1001,
                  reason="7天无理由", status=RefundStatus.COMPLETED.value,
                  amount=300, create_time=now + datetime.timedelta(days=2),
                  update_time=now + datetime.timedelta(days=2)),
        RefundORM(id=9, refund_no="RF_OTHER", order_id=9, user_id=2002,
                  reason="他人退款", status=RefundStatus.PENDING.value,
                  amount=50, create_time=now, update_time=now),
    ]
    s.add_all(orders + refunds)
    s.commit()

    @contextmanager
    def fake_safe_session(commit=True):
        yield s

    with patch.object(mysql_impl, "with_safe_session", fake_safe_session):
        yield s
    s.close()
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


# =============================================================
# #1-#2 RefundService MySQL 实现
# =============================================================
def test_refund_mysql_impl_get_refund(seeded_session):
    """#1 mock session → Refund 字段全对（含 order_no 注入）"""
    svc = MySQLRefundService()
    refund = _run(svc.get_refund(1001, "RF001"))
    assert refund is not None
    assert refund.refund_no == "RF001"
    assert refund.order_no == "ORD001"           # JOIN 注入
    assert refund.user_id == 1001
    assert refund.status == "pending"
    assert refund.amount == 100.0
    assert refund.reason == "不喜欢"
    assert refund.create_time == datetime.datetime(2026, 7, 1, 10, 0, 0)


def test_refund_mysql_impl_get_refund_not_found(seeded_session):
    """#2 退款不存在 / 越权 → None"""
    svc = MySQLRefundService()
    assert _run(svc.get_refund(1001, "NOPE")) is None
    # 越权：RF_OTHER 属于 2002，用 1001 查应为 None
    assert _run(svc.get_refund(1001, "RF_OTHER")) is None


# =============================================================
# #3-#5 list_user_refunds 全量 / 状态过滤 / 分页
# =============================================================
def test_refund_mysql_impl_list_user_refunds(seeded_session):
    """#3 多退款 + cursor 分页（DESC 排序 + next_cursor）"""
    svc = MySQLRefundService()
    page1, cursor = _run(svc.list_user_refunds(1001, limit=2))
    assert [r.refund_no for r in page1] == ["RF003", "RF002"]  # create_time DESC
    assert cursor is not None
    page2, cursor2 = _run(svc.list_user_refunds(1001, limit=2, cursor=cursor))
    assert [r.refund_no for r in page2] == ["RF001"]
    assert cursor2 is None                                       # 无更多
    # order_no 注入
    assert all(r.order_no is not None for r in page1 + page2)


def test_refund_mysql_impl_filter_by_status(seeded_session):
    """#4 status 过滤生效"""
    svc = MySQLRefundService()
    refunds, _ = _run(svc.list_user_refunds(1001, status="approved"))
    assert len(refunds) == 1
    assert refunds[0].refund_no == "RF002"
    # 多状态
    pending, _ = _run(svc.list_user_refunds(1001, status="pending"))
    assert len(pending) == 1
    assert pending[0].refund_no == "RF001"


def test_refund_mysql_impl_cursor_pagination(seeded_session):
    """#5 cursor 分页边界 — limit=1 时严格返回 1 条 + next_cursor"""
    svc = MySQLRefundService()
    refunds, cursor = _run(svc.list_user_refunds(1001, limit=1))
    assert len(refunds) == 1
    assert refunds[0].refund_no == "RF003"
    assert cursor is not None
    refunds2, cursor2 = _run(svc.list_user_refunds(1001, limit=1, cursor=cursor))
    assert len(refunds2) == 1
    assert refunds2[0].refund_no == "RF002"


# =============================================================
# #6 get_refund_status
# =============================================================
def test_refund_mysql_impl_get_refund_status(seeded_session):
    """#6 get_refund_status 返字符串 / None"""
    svc = MySQLRefundService()
    assert _run(svc.get_refund_status("RF001")) == "pending"
    assert _run(svc.get_refund_status("RF002")) == "approved"
    assert _run(svc.get_refund_status("RF003")) == "completed"
    assert _run(svc.get_refund_status("NOPE")) is None


# =============================================================
# #7-#8 Tool 改用 Protocol（mock 替换）
# =============================================================
def test_refund_tool_list_uses_protocol():
    """#7 RefundTool.list_user_refunds 走 RefundService Protocol（mock 替换即生效）"""
    from app.schemas.business import Refund as RefundDTO
    from app.tools.refund_tool import RefundTool

    now = datetime.datetime(2026, 7, 1, 10, 0, 0)
    fake_refunds = [
        RefundDTO(refund_no="RF001", order_no="ORD001", user_id=1001,
                  status="pending", amount=100.0, reason="不喜欢",
                  remark=None, create_time=now, update_time=now),
        RefundDTO(refund_no="RF002", order_no="ORD002", user_id=1001,
                  status="approved", amount=200.0, reason="质量问题",
                  remark="已批准", create_time=now, update_time=now),
    ]
    mock_svc = AsyncMock()
    mock_svc.list_user_refunds.return_value = (fake_refunds, None)
    mock_factory = type("F", (), {"get_refund_service": lambda self: mock_svc})()

    with patch("app.tools.refund_tool.get_refund_service_factory", return_value=mock_factory):
        result = RefundTool.list_user_refunds(1001, limit=20)

    # 委托给 Protocol（limit=20 透传）
    mock_svc.list_user_refunds.assert_awaited_once()
    args, kwargs = mock_svc.list_user_refunds.call_args
    assert args[0] == 1001
    assert kwargs.get("limit") == 20 or (len(args) >= 2 and args[1] == 20)

    # dict 字段与旧 _to_dict 兼容
    assert len(result) == 2
    assert result[0]["refund_no"] == "RF001"
    assert result[0]["status"] == "pending"
    assert result[0]["amount"] == 100.0
    assert result[0]["order_no"] == "ORD001"        # 新增字段（向后兼容）


def test_refund_tool_get_by_no_uses_protocol():
    """#8 RefundTool.get_refund_by_no 走 RefundService Protocol"""
    from app.schemas.business import Refund as RefundDTO
    from app.tools.refund_tool import RefundTool

    now = datetime.datetime(2026, 7, 1, 10, 0, 0)
    fake_refund = RefundDTO(
        refund_no="RF001", order_no="ORD001", user_id=1001,
        status="pending", amount=100.0, reason="不喜欢",
        remark=None, create_time=now, update_time=now,
    )
    mock_svc = AsyncMock()
    mock_svc.get_refund.return_value = fake_refund
    mock_factory = type("F", (), {"get_refund_service": lambda self: mock_svc})()

    with patch("app.tools.refund_tool.get_refund_service_factory", return_value=mock_factory):
        result = RefundTool.get_refund_by_no(1001, "RF001")

    mock_svc.get_refund.assert_awaited_once_with(1001, "RF001")
    assert result is not None
    assert result["refund_no"] == "RF001"
    assert result["order_no"] == "ORD001"
    assert result["status"] == "pending"


# =============================================================
# Factory 一致性（额外验证，非 spec 列表但 cheap）
# =============================================================
def test_factory_returns_mysql_impl():
    """get_refund_service() → MySQLRefundService + runtime_checkable"""
    from app.services.refund.factory import get_refund_service_factory

    factory = get_refund_service_factory()
    assert isinstance(factory.get_refund_service(), MySQLRefundService)
    assert isinstance(factory.get_refund_service(), RefundService)