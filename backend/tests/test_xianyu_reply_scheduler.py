"""
ReplyScheduler 测试 — 随机延迟 + Agent 调 + send + 限流 + 去重

按 CLAUDE.md 调试铁律：测试 mock BuyerProfile / PhaseDetector / Agent API / XianyuMCPClient
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.xianyu.reply_scheduler import ReplyScheduler


# =============================================================
# 1. 随机延迟
# =============================================================
class TestRandomDelay:
    def test_delay_within_range(self):
        """随机延迟在 [min_seconds, max_seconds] 区间内"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
            min_delay_seconds=0.05,
            max_delay_seconds=0.1,
        )
        # 测 5 次，每次都在区间
        for _ in range(5):
            t = scheduler._compute_delay()
            assert 0.05 <= t <= 0.1


# =============================================================
# 2. 限流
# =============================================================
class TestRateLimit:
    def test_first_message_under_limit(self):
        """首条消息不受限"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
            rate_limit_per_minute=1,
        )
        assert scheduler._check_rate_limit() is True

    def test_1_per_minute_limit(self):
        """1/分钟 限流：1 条后立即发第 2 条被拒"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
            rate_limit_per_minute=1,
        )
        scheduler._record_sent()
        assert scheduler._check_rate_limit() is False

    def test_daily_quota(self):
        """日上限 5/日：6 条后被拒"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
            rate_limit_per_minute=100,  # 不触发分钟限流
            daily_quota=5,
        )
        for _ in range(5):
            scheduler._record_sent()
        assert scheduler._check_rate_limit() is False  # 第 6 条被拒


# =============================================================
# 3. 消息去重
# =============================================================
class TestDedup:
    def test_dedup_by_message_id(self):
        """同一 message_id 重复触发 → 跳过"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        scheduler._seen_message_ids.add("m1")
        assert scheduler._is_duplicate({"message_id": "m1"}) is True
        assert scheduler._is_duplicate({"message_id": "m2"}) is False

    def test_message_without_id_not_deduped(self):
        """无 message_id 事件 → 不去重（保守行为）"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        assert scheduler._is_duplicate({"message_id": ""}) is False


# =============================================================
# 4. send_message 失败重试
# =============================================================
class TestRetry:
    def test_send_failure_records_error(self):
        """send_message 返 error → 记录 + 不抛"""
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"error": "mcp_call_timeout"}
        scheduler = ReplyScheduler(
            xianyu_client=mock_client,
            chat_api_url="http://test",
            max_retries=1,
        )
        result = scheduler._send_with_retry(
            cid="c1", toid="u1", text="hi"
        )
        assert result["error"] == "mcp_call_timeout"
        assert mock_client.send_message.call_count == 2  # 重试 1 次

    def test_send_success_no_retry(self):
        """send_message 成功 → 不重试"""
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"ok": True, "message_id": "M1"}
        scheduler = ReplyScheduler(
            xianyu_client=mock_client,
            chat_api_url="http://test",
            max_retries=3,
        )
        result = scheduler._send_with_retry(
            cid="c1", toid="u1", text="hi"
        )
        assert result.get("ok") is True
        assert mock_client.send_message.call_count == 1


# =============================================================
# 5. 智能客服 /chat 调用
# =============================================================
class TestChatApiCall:
    def test_call_chat_api_success(self):
        """/chat 调通 → 返 reply"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test/api/chat",
        )
        # mock httpx.post 返 SSE
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.text = (
            'data: {"answer": "在的，您是想了解注塑模具吗？"}\n\n'
            'data: [DONE]\n\n'
        )
        with patch("httpx.post", return_value=fake_response) as mock_post:
            result = scheduler._call_chat_api(
                unb="u1",
                message="在吗",
                context="【闲鱼买家】nick=zwyyy",
            )
        assert "在的" in result

    def test_call_chat_api_failure_returns_empty(self):
        """/chat 失败 → 返空字符串（不抛）"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test/api/chat",
        )
        with patch("httpx.post", side_effect=Exception("network down")):
            result = scheduler._call_chat_api(unb="u1", message="x", context="")
        assert result == ""


# =============================================================
# 6. 状态
# =============================================================
class TestState:
    def test_sent_count(self):
        """sent_count 返今日已发条数"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        assert scheduler.sent_count() == 0
        scheduler._record_sent()
        scheduler._record_sent()
        assert scheduler.sent_count() == 2