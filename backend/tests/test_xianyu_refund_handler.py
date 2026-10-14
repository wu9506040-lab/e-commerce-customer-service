"""
XianyuRefundHandler V16.3 测试 — 基于 DB 拒绝 + 转人工

按 CLAUDE.md 调试铁律：SQLite in-memory
"""
import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models import xianyu_order  # noqa
from app.services.xianyu.order_repository import XianyuOrderRepository
from app.services.xianyu.refund_handler import (
    XianyuRefundHandler,
    RefundDecision,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


# =============================================================
# 1. 已退款订单 → 直接拒绝（关键安全）
# =============================================================
class TestRefundedOrder:
    def test_refunded_order_rejected(self, db_session):
        """DB 有 status=refunded → 直接拒绝（带证据）"""
        # 准备：已退款订单
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "refunded", "退款成功", "m2"
        )
        db_session.commit()

        handler = XianyuRefundHandler(db=db_session, seller_unb="s1")
        decision = handler.handle_refund_request(
            unb="u1", buyer_nick="zwyyy"
        )
        assert decision.should_reject is True
        assert decision.should_escalate is False
        assert decision.reject_reason == "已退款"
        assert decision.order_state == "refunded"
        assert decision.order_no == "O1"
        # 回复里应有"已退款"（或"退款成功"）+买家昵称
        assert ("已退款" in decision.reply_text or "退款成功" in decision.reply_text)
        assert "zwyyy" in decision.reply_text

    def test_rejection_includes_appeal_channel(self, db_session):
        """拒绝话术里给买家申诉渠道（闲鱼官方客服）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "refunded", "refund", "m2"
        )
        db_session.commit()

        handler = XianyuRefundHandler(db=db_session, seller_unb="s1")
        decision = handler.handle_refund_request(unb="u1", buyer_nick="n")
        # 应有官方客服电话或引导
        assert "9510202" in decision.reply_text or "App" in decision.reply_text


# =============================================================
# 2. 未退款订单 → 转人工
# =============================================================
class TestActiveOrder:
    def test_paid_order_escalate(self, db_session):
        """已付款未发货 → 转人工（normal priority）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        db_session.commit()

        handler = XianyuRefundHandler(db=db_session, seller_unb="s1")
        decision = handler.handle_refund_request(unb="u1", buyer_nick="n")
        assert decision.should_reject is False
        assert decision.should_escalate is True
        assert decision.escalation_priority == "normal"
        assert decision.order_state == "paid"
        assert "登记" in decision.reply_text or "专员" in decision.reply_text

    def test_signed_order_escalate_urgent(self, db_session):
        """已签收 → 转人工（urgent priority）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "shipped", "s", "m2"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "signed", "g", "m3"
        )
        db_session.commit()

        handler = XianyuRefundHandler(db=db_session, seller_unb="s1")
        decision = handler.handle_refund_request(unb="u1", buyer_nick="n")
        assert decision.should_escalate is True
        assert decision.escalation_priority == "urgent"
        assert decision.order_state == "signed"

    def test_received_order_escalate_urgent(self, db_session):
        """已收货 → 转人工（urgent，质量问题窗口）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "received", "r", "m2"
        )
        db_session.commit()

        handler = XianyuRefundHandler(db=db_session, seller_unb="s1")
        decision = handler.handle_refund_request(unb="u1", buyer_nick="n")
        assert decision.should_escalate is True
        assert decision.escalation_priority == "urgent"


# =============================================================
# 3. 无订单记录 → 转人工（安全）
# =============================================================
class TestNoOrder:
    def test_no_order_record_escalate_normal(self, db_session):
        """DB 无该买家记录 → 转人工（normal）"""
        handler = XianyuRefundHandler(db=db_session, seller_unb="s1")
        decision = handler.handle_refund_request(unb="u1", buyer_nick="n")
        assert decision.should_reject is False
        assert decision.should_escalate is True
        assert decision.escalation_priority == "normal"
        assert decision.order_state == "unknown"
        assert "登记" in decision.reply_text or "专员" in decision.reply_text


# =============================================================
# 4. RefundDecision 字段
# =============================================================
class TestDecisionFields:
    def test_fields_exist(self):
        """RefundDecision 字段完整"""
        d = RefundDecision(
            should_reject=False,
            should_escalate=True,
            escalation_priority="normal",
            reply_text="test",
            order_state="paid",
        )
        assert d.should_reject is False
        assert d.should_escalate is True
        assert d.escalation_priority == "normal"
        assert d.reply_text == "test"
        assert d.order_state == "paid"
        # reject_reason / order_no / order_updated_at 可选
        assert d.reject_reason == ""
        assert d.order_no is None


# =============================================================
# 5. buyer_nick 注入
# =============================================================
class TestNickInjection:
    def test_no_nick_still_works(self, db_session):
        """没买家昵称也能工作（reply 用 fallback）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        db_session.commit()
        handler = XianyuRefundHandler(db=db_session, seller_unb="s1")
        decision = handler.handle_refund_request(unb="u1", buyer_nick="")
        # reply 仍然有意义
        assert len(decision.reply_text) > 5
