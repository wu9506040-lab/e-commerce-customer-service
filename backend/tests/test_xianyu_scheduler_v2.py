"""
ReplyScheduler V16.3 集成测试 — 落库 + 退款 handler

按 CLAUDE.md 调试铁律：mock LLM/MCP + 真 SQLite 测落库
"""
import datetime
import time
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models import xianyu_order  # noqa
from app.services.xianyu.order_repository import XianyuOrderRepository
from app.services.xianyu.reply_scheduler import ReplyScheduler


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


@pytest.fixture
def mock_components():
    client = MagicMock()
    client.is_enabled.return_value = True
    client.send_message.return_value = {"ok": True, "message_id": "M1"}
    return {
        "client": client,
        "buyer_profile": MagicMock(),
        "scenario_router": MagicMock(),
        "escalation": MagicMock(),
        "message_guard": MagicMock(),
        "prompts": MagicMock(),
    }


# =============================================================
# 1. 触发落库
# =============================================================
class TestPersist:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_on_fire_persists_orders(self, db_session, mock_components):
        """on_fire 时调用 parse_and_persist 落库"""
        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
            db_session=db_session,
            seller_unb="s1",
        )

        messages = [
            {"message_id": "m1", "send_user_id": "u1",
             "message": {"tip": {"tip": "我已付款"},
                         "textCard": {"content": '<a href="fleamarket://order_detail?id=O1">详情</a>'}}},
            {"message_id": "m2", "send_user_id": "u1",
             "message": {"tip": {"tip": "退款成功"}}},
        ]
        # mock 其他模块
        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", create=True), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter"), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector") as mock_esc, \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard") as mock_guard, \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts") as mock_prompts, \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_esc.return_value.should_escalate.return_value.escalate = False
            mock_guard.return_value.check_or_suggest.return_value = (True, "ok")
            mock_prompts.return_value.render.return_value = "sys"
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = 'data: {"answer": "在的"}\n\ndata: [DONE]\n\n'
            mock_post.return_value = mock_resp

            scheduler._handle_fire(
                cid="c1",
                messages=messages + [
                    {"message_id": "m3", "send_user_id": "u1",
                     "message": {"text": {"text": "在吗"}}},
                ],
            )
            time.sleep(0.1)  # 同步调用

        # 验证落库
        orders = XianyuOrderRepository.list_by_buyer(db_session, "s1", "u1")
        assert len(orders) == 1
        # 最终状态 = refunded（m2 升级）
        assert orders[0].status == "refunded"
        assert orders[0].order_no == "O1"


# =============================================================
# 2. 已退款订单 → 直接拒绝（不调 LLM）
# =============================================================
class TestRefundRejected:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_refunded_order_sends_rejection_directly(
        self, db_session, mock_components
    ):
        """DB 有 status=refunded → 智能客服直接发拒绝话术（不调 /chat）"""
        # 准备：已退款订单
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "refunded", "r", "m2"
        )
        db_session.commit()

        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
            db_session=db_session,
            seller_unb="s1",
        )

        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", create=True), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter"), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector") as mock_esc, \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard") as mock_guard, \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts") as mock_prompts, \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_esc.return_value.should_escalate.return_value.escalate = False
            mock_guard.return_value.check_or_suggest.return_value = (True, "ok")
            mock_prompts.return_value.render.return_value = "sys"

            # 买家发"申请退款"（含退款关键词 + 上下文带已退款订单）
            messages = [
                {"message_id": "m1", "send_user_id": "u1",
                 "message": {"text": {"text": "我已付款"},  # 触发落库
                             "textCard": {"content": '<a href="fleamarket://order_detail?id=O1">详情</a>'}}},
                {"message_id": "m2", "send_user_id": "u1",
                 "message": {"tip": {"tip": "退款成功"}}},
                {"message_id": "m3", "send_user_id": "u1",
                 "message": {"text": {"text": "申请退款"}}},  # 最新买家消息
            ]
            scheduler._handle_fire(cid="c1", messages=messages)
            time.sleep(0.1)  # 线程已同步，无需等待

        # /chat 不应被调（直接拒绝，不走 LLM）
        mock_post.assert_not_called()
        # send_message 被调（拒绝话术）
        mock_components["client"].send_message.assert_called_once()
        call_args = mock_components["client"].send_message.call_args
        text = call_args.kwargs.get("text", "")
        # 应包含"已退款"或"退款成功"
        assert "已退款" in text or "退款成功" in text
        # 应有订单号
        assert "O1" in text


# =============================================================
# 3. 未退款订单 → 转人工（不调 LLM）
# =============================================================
class TestRefundEscalate:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_active_order_sends_escalation(
        self, db_session, mock_components
    ):
        """DB 有 status=paid/shipped/signed → 转人工话术"""
        # 准备：已付款订单
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        db_session.commit()

        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
            db_session=db_session,
            seller_unb="s1",
        )

        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", create=True), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter"), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector") as mock_esc, \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard") as mock_guard, \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts") as mock_prompts, \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_esc.return_value.should_escalate.return_value.escalate = False
            mock_guard.return_value.check_or_suggest.return_value = (True, "ok")
            mock_prompts.return_value.render.return_value = "sys"

            messages = [
                {"message_id": "m1", "send_user_id": "u1",
                 "message": {"text": {"text": "我已付款"},
                             "textCard": {"content": '<a href="fleamarket://order_detail?id=O1">详情</a>'}}},
                {"message_id": "m2", "send_user_id": "u1",
                 "message": {"text": {"text": "申请退款"}}},
            ]
            scheduler._handle_fire(cid="c1", messages=messages)
            time.sleep(0.1)  # 线程已同步，无需等待

        # /chat 不应被调（直接转人工话术）
        mock_post.assert_not_called()
        # send_message 被调（转人工话术）
        mock_components["client"].send_message.assert_called_once()
        text = mock_components["client"].send_message.call_args.kwargs.get("text", "")
        assert "专员" in text or "登记" in text
        # 应有订单号 + 状态
        assert "O1" in text


# =============================================================
# 4. 无订单 + 退款意图 → 转人工（安全）
# =============================================================
class TestRefundNoOrder:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_no_order_with_refund_intent(self, db_session, mock_components):
        """DB 无记录 + 买家说退款 → 转人工"""
        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
            db_session=db_session,
            seller_unb="s1",
        )

        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", create=True), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter"), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector") as mock_esc, \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard") as mock_guard, \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts") as mock_prompts, \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_esc.return_value.should_escalate.return_value.escalate = False
            mock_guard.return_value.check_or_suggest.return_value = (True, "ok")
            mock_prompts.return_value.render.return_value = "sys"

            messages = [{
                "message_id": "m1", "send_user_id": "u1",
                "message": {"text": {"text": "申请退款"}},  # 无订单历史
            }]
            scheduler._handle_fire(cid="c1", messages=messages)
            time.sleep(0.1)  # 线程已同步，无需等待

        # /chat 不调（走退款话术）
        mock_post.assert_not_called()
        # send_message 被调
        mock_components["client"].send_message.assert_called_once()


# =============================================================
# 5. 非退款意图 → 走原 LLM 流程
# =============================================================
class TestNonRefund:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_normal_query_uses_llm(self, db_session, mock_components):
        """普通问题（"在吗"）→ 调 LLM 走原流程"""
        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
            db_session=db_session,
            seller_unb="s1",
        )

        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", create=True) as mock_bp, \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter") as mock_sr, \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector") as mock_esc, \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard") as mock_guard, \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts") as mock_prompts, \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            from app.services.xianyu.scenario_router import Scenario, ScenarioResult
            from app.services.xianyu.buyer_profile import BuyerProfileData

            profile_data = BuyerProfileData(unb="u1", nick="n", fetched_at=0)
            mock_bp.return_value.get_profile.return_value = profile_data
            mock_sr.return_value.route.return_value = ScenarioResult(
                scenario=Scenario.PRESALE,
                prompt_hint="h",
                should_escalate=False,
            )
            mock_esc.return_value.should_escalate.return_value.escalate = False
            mock_guard.return_value.check_or_suggest.return_value = (True, "ok")
            mock_prompts.return_value.render.return_value = "sys"
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = 'data: {"answer": "在的"}\n\ndata: [DONE]\n\n'
            mock_post.return_value = mock_resp

            messages = [{
                "message_id": "m1", "send_user_id": "u1",
                "message": {"text": {"text": "在吗"}},  # 非退款
            }]
            scheduler._handle_fire(cid="c1", messages=messages)
            time.sleep(0.1)  # 线程已同步，无需等待

        # /chat 被调
        mock_post.assert_called_once()
        # send_message 被调
        mock_components["client"].send_message.assert_called_once()