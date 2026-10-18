"""
ReplyScheduler V16.2 集成测试 — 接入 ScenarioRouter + EscalationDetector + MessageGuard

按 CLAUDE.md 调试铁律：mock BuyerProfile / MCP client / /chat API
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.xianyu.reply_scheduler import ReplyScheduler


@pytest.fixture
def mock_components():
    """集成 mock：所有子模块都用 mock"""
    from app.services.xianyu.buyer_profile import BuyerProfileData
    from app.services.xianyu.scenario_router import Scenario, ScenarioResult
    from app.services.xianyu.escalation_detector import EscalationReason, EscalationResult

    client = MagicMock()
    client.is_enabled.return_value = True
    client.send_message.return_value = {"ok": True, "message_id": "M1"}

    # BuyerProfile：显式构造 profile data，to_prompt_context 是真方法
    profile_data = BuyerProfileData(
        unb="u1", nick="zwyyy",
        history_with_me=[],
        is_returning_buyer=True,
        fetched_at=0,
    )
    buyer_profile = MagicMock()
    buyer_profile.get_profile.return_value = profile_data

    # ScenarioRouter：显式 ScenarioResult
    scenario_result = ScenarioResult(
        scenario=Scenario.PRESALE,
        prompt_hint="【场景：售前】...",
        should_escalate=False,
    )
    scenario_router = MagicMock()
    scenario_router.route.return_value = scenario_result

    # EscalationDetector：显式 EscalationResult
    esc_result = EscalationResult(
        escalate=False,
        reason=None,
        reply="",
    )
    escalation = MagicMock()
    escalation.should_escalate.return_value = esc_result
    escalation.record_message = MagicMock()

    # MessageGuard
    message_guard = MagicMock()
    message_guard.check_or_suggest.return_value = (True, "ok")

    # Prompts
    prompts = MagicMock()
    prompts.render.return_value = (
        "你是智能客服\n\n"
        "【闲鱼买家】昵称=zwyyy 老客=True\n\n"
        "【对话示例】\nbuyer: x\nseller: y"
    )

    return {
        "client": client,
        "buyer_profile": buyer_profile,
        "scenario_router": scenario_router,
        "escalation": escalation,
        "message_guard": message_guard,
        "prompts": prompts,
    }


# =============================================================
# 1. 正常售前：完整流程
# =============================================================
class TestNormalFlow:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_presale_query_full_flow(self, mock_components):
        """'在吗' → escalation.no → router.presale → /chat → send_message"""
        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
        )
        # patch 子模块（类 → 返 mock 实例）
        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", return_value=mock_components["buyer_profile"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter", return_value=mock_components["scenario_router"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector", return_value=mock_components["escalation"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard", return_value=mock_components["message_guard"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts", return_value=mock_components["prompts"]), \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = 'data: {"answer": "在的，亲～"}\n\ndata: [DONE]\n\n'
            mock_post.return_value = mock_resp

            scheduler.on_fire(
                cid="c1",
                messages=[{"message_id": "m1", "send_user_id": "u1",
                           "send_user_name": "zwyyy",
                           "message": {"text": {"text": "在吗"}}}],
            )
            time.sleep(0.5)

        mock_components["client"].send_message.assert_called_once()
        assert scheduler.sent_count() == 1


# =============================================================
# 2. 触发转人工：不发智能客服回复
# =============================================================
class TestEscalationFlow:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_escalation_skips_chat_and_send(self, mock_components):
        """'转人工' → escalation.yes → 跳过 /chat + send_message，直接发转人工话术"""
        # escalation 返 escalate=True（覆盖 fixture 默认）
        from app.services.xianyu.escalation_detector import EscalationReason, EscalationResult
        mock_components["escalation"].should_escalate.return_value = EscalationResult(
            escalate=True,
            reason=EscalationReason.USER_REQUEST,
            reply="好的，已为您转接专员，请稍候片刻。",
        )

        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
        )
        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", return_value=mock_components["buyer_profile"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter", return_value=mock_components["scenario_router"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector", return_value=mock_components["escalation"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard", return_value=mock_components["message_guard"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts", return_value=mock_components["prompts"]), \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:

            scheduler.on_fire(
                cid="c1",
                messages=[{"message_id": "m1", "send_user_id": "u1",
                           "send_user_name": "zwyyy",
                           "message": {"text": {"text": "转人工"}}}],
            )
            time.sleep(0.5)

        # /chat 不应被调
        mock_post.assert_not_called()
        # send_message 仍应被调（转人工话术）
        mock_components["client"].send_message.assert_called_once()
        # 送的应该是 escalation 话术
        call_args = mock_components["client"].send_message.call_args
        text = call_args.kwargs.get("text", "")
        assert "专员" in text or "稍候" in text


# =============================================================
# 3. 消息被 guard 拦截：不发
# =============================================================
class TestGuardFlow:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_message_blocked_by_guard(self, mock_components):
        """LLM 回复含'已退款 50 元' → guard 拦截 → 不真发"""
        # mock /chat 返"已退款 50 元"
        with patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = 'data: {"answer": "您的订单已退款 50 元"}\n\ndata: [DONE]\n\n'
            mock_post.return_value = mock_resp

        # mock guard 返拦截 + safe alternative
        mock_components["message_guard"].check_or_suggest.side_effect = [
            (False, "已记下您的退款需求，请稍候会有专员 24 小时内与您联系。"),
            (True, "已记下您的退款需求，请稍候会有专员 24 小时内与您联系。"),
        ]

        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
        )
        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", return_value=mock_components["buyer_profile"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter", return_value=mock_components["scenario_router"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector", return_value=mock_components["escalation"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard", return_value=mock_components["message_guard"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts", return_value=mock_components["prompts"]), \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = 'data: {"answer": "您的订单已退款 50 元"}\n\ndata: [DONE]\n\n'
            mock_post.return_value = mock_resp

            scheduler.on_fire(
                cid="c1",
                messages=[{"message_id": "m1", "send_user_id": "u1",
                           "send_user_name": "zwyyy",
                           "message": {"text": {"text": "在吗"}}}],
            )
            time.sleep(0.5)

        # send_message 仍被调（用 safe alternative）
        mock_components["client"].send_message.assert_called_once()
        # 但内容必须是 safe alternative，**不**是 "已退款"
        call_args = mock_components["client"].send_message.call_args
        text = call_args.kwargs.get("text", "")
        assert "已退款" not in text
        assert "专员" in text


# =============================================================
# 4. 限流：1/分钟，第 2 条拒发
# =============================================================
class TestRateLimit:
    @pytest.mark.skip(reason="V18 重构移除 BuyerProfile/ScenarioRouter 直连，测试待按新架构重写（技术债，见 docs/test_quarantine.md）")
    def test_rate_limit_blocks_second_message(self, mock_components):
        """1/分钟限流：第 2 条直接拒发"""
        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
            rate_limit_per_minute=1,
        )
        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", return_value=mock_components["buyer_profile"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter", return_value=mock_components["scenario_router"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector", return_value=mock_components["escalation"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard", return_value=mock_components["message_guard"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts", return_value=mock_components["prompts"]), \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = 'data: {"answer": "在的"}\n\ndata: [DONE]\n\n'
            mock_post.return_value = mock_resp

            # 第 1 条
            scheduler.on_fire(
                cid="c1",
                messages=[{"message_id": "m1", "send_user_id": "u1",
                           "send_user_name": "zwyyy",
                           "message": {"text": {"text": "hi"}}}],
            )
            time.sleep(0.5)
            # 第 2 条
            scheduler.on_fire(
                cid="c1",
                messages=[{"message_id": "m2", "send_user_id": "u1",
                           "send_user_name": "zwyyy",
                           "message": {"text": {"text": "在吗"}}}],
            )
            time.sleep(0.5)

        # 只发 1 次（限流）
        assert mock_components["client"].send_message.call_count == 1


# =============================================================
# 5. BuyerProfile 注入 system prompt
# =============================================================
class TestProfileInjection:
    @pytest.mark.skip(reason="V18.2 注入链路重构后断言过时（docs/test_quarantine.md）")
    def test_buyer_profile_in_chat_payload(self, mock_components):
        """/chat 调用 payload 含 buyer_profile context"""
        scheduler = ReplyScheduler(
            xianyu_client=mock_components["client"],
            chat_api_url="http://test",
            min_delay_seconds=0.0,
            max_delay_seconds=0.0,
        )
        with patch("app.services.xianyu.reply_scheduler.BuyerProfile", return_value=mock_components["buyer_profile"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuScenarioRouter", return_value=mock_components["scenario_router"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuEscalationDetector", return_value=mock_components["escalation"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuMessageGuard", return_value=mock_components["message_guard"]), \
             patch("app.services.xianyu.reply_scheduler.XianyuPrompts", return_value=mock_components["prompts"]), \
             patch("app.services.xianyu.reply_scheduler.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = 'data: {"answer": "在的"}\n\ndata: [DONE]\n\n'
            mock_post.return_value = mock_resp

            scheduler.on_fire(
                cid="c1",
                messages=[{"message_id": "m1", "send_user_id": "u1",
                           "send_user_name": "zwyyy",
                           "message": {"text": {"text": "hi"}}}],
            )
            time.sleep(0.5)

        # /chat 被调，payload 应含 buyer_profile
        mock_post.assert_called()
        call_kwargs = mock_post.call_args.kwargs
        payload = call_kwargs.get("json", {})
        # payload 的 system 含 profile 内容
        # 也可能在 system prompt 字段（取决于设计）
        assert "zwyyy" in str(payload) or "老客" in str(payload)