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

    def test_rate_limit_parse_wait_seconds(self):
        """限流响应 message 含 '再等 53.1s' → 解析出 53.1+buffer"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        result = {
            "ok": False,
            "error_type": "RateLimitedError",
            "message": "限流：bucket=message.write 每 60s 上限 1，再等 53.1s",
        }
        wait = scheduler._parse_rate_limit_wait(result)
        assert wait is not None
        # 53.1 + 0.5 buffer = 53.6
        assert 53.0 <= wait <= 54.5

    def test_rate_limit_non_rate_limit_returns_none(self):
        """非限流错误 → 返 None（不视为限流）"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        # 普通错误
        result = {"ok": False, "error_type": "NetworkError", "message": "timeout"}
        assert scheduler._parse_rate_limit_wait(result) is None
        # 完全没有 error_type
        result = {"ok": False, "message": "something"}
        assert scheduler._parse_rate_limit_wait(result) is None
        # message 是 None
        result = {"ok": False, "error_type": "RateLimitedError", "message": None}
        assert scheduler._parse_rate_limit_wait(result) is None

    def test_rate_limit_parse_handles_garbage(self):
        """限流响应 message 解析不出秒数 → 返 None（兜底）"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        result = {
            "ok": False,
            "error_type": "RateLimitedError",
            "message": "限流 but no seconds info",
        }
        assert scheduler._parse_rate_limit_wait(result) is None

    def test_send_rate_limit_waits_then_succeeds(self):
        """限流场景：第一次返限流，第二次成功 → 总共调 2 次"""
        mock_client = MagicMock()
        # 第一次限流，第二次成功
        mock_client.send_message.side_effect = [
            {
                "ok": False,
                "error_type": "RateLimitedError",
                "message": "限流：再等 0.1s",  # 短 sleep 加速
            },
            {"ok": True, "message_id": "M1"},
        ]
        scheduler = ReplyScheduler(
            xianyu_client=mock_client,
            chat_api_url="http://test",
            max_retries=0,  # 非限流无重试
        )
        with patch("time.sleep") as mock_sleep:  # mock sleep 避免真等
            result = scheduler._send_with_retry(
                cid="c1", toid="u1", text="hi"
            )
        assert result.get("ok") is True
        assert mock_client.send_message.call_count == 2
        # 第一次 sleep 应是限流解析出的 ~0.6s (0.1 + 0.5)
        first_sleep = mock_sleep.call_args_list[0][0][0]
        assert 0.5 <= first_sleep <= 1.5

    def test_send_rate_limit_keeps_retrying_until_quota_exhausted(self):
        """限流 5 次都用完仍限流 → 返限流 result（不抛）"""
        mock_client = MagicMock()
        # 永远返限流
        mock_client.send_message.return_value = {
            "ok": False,
            "error_type": "RateLimitedError",
            "message": "限流：再等 0.1s",
        }
        scheduler = ReplyScheduler(
            xianyu_client=mock_client,
            chat_api_url="http://test",
            max_retries=0,
        )
        with patch("time.sleep"):
            result = scheduler._send_with_retry(
                cid="c1", toid="u1", text="hi"
            )
        # 1 次非限流尝试 + 5 次限流尝试 = 6 次
        assert mock_client.send_message.call_count == 6
        assert result.get("ok") is False
        assert result.get("error_type") == "RateLimitedError"

    def test_send_non_rate_limit_keeps_old_exponential_backoff(self):
        """非限流错误：维持 1s/2s 指数退避（原行为不变）"""
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "ok": False,
            "error_type": "NetworkError",
            "message": "timeout",
        }
        scheduler = ReplyScheduler(
            xianyu_client=mock_client,
            chat_api_url="http://test",
            max_retries=2,  # 3 次尝试
        )
        with patch("time.sleep") as mock_sleep:
            result = scheduler._send_with_retry(
                cid="c1", toid="u1", text="hi"
            )
        # 3 次尝试，2 次 sleep（1s + 2s）
        assert mock_client.send_message.call_count == 3
        assert mock_sleep.call_count == 2
        # 第 1 次 sleep 应是 1s（2^0 * 1.0）
        assert mock_sleep.call_args_list[0][0][0] == 1.0
        # 第 2 次 sleep 应是 2s（2^1 * 1.0）
        assert mock_sleep.call_args_list[1][0][0] == 2.0
        assert result.get("ok") is False


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


# =============================================================
# 7. 多消息合并（V16.7：debounce 12s 内的多条 → 一条综合回复）
# =============================================================
class TestMultiMessageMerge:
    """用户连发多条 → 拼成单条 query → LLM 一次性综合回答 → 一次发送"""

    def _msg(self, text: str, msg_id: str = "") -> dict:
        """造一个标准 message 事件"""
        return {
            "event": "message",
            "cid": "c1",
            "send_user_id": "u1",
            "send_message": text,
            "message_id": msg_id,
        }

    def test_build_query_single_message_unchanged(self):
        """单条消息 → query 就是原文（向后兼容）"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        msgs = [self._msg("在吗", "m1")]
        query, is_multi = scheduler._build_query(msgs)
        assert query == "在吗"
        assert is_multi is False

    def test_build_query_multiple_messages_merged(self):
        """多条消息 → 拼成综合 prompt（按用户指定格式）"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        msgs = [
            self._msg("在吗", "m1"),
            self._msg("这个充电宝多大容量", "m2"),
            self._msg("能便宜点吗", "m3"),
        ]
        query, is_multi = scheduler._build_query(msgs)
        # 验证格式（按用户 spec）
        assert "用户连续发了以下几条消息" in query
        assert "1. 在吗" in query
        assert "2. 这个充电宝多大容量" in query
        assert "3. 能便宜点吗" in query
        assert "请你综合回答这几个问题" in query
        assert "一条回复里覆盖所有问题" in query
        assert is_multi is True

    def test_build_query_skips_empty_messages(self):
        """空消息（提取不出文本）→ 跳过"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        msgs = [
            self._msg("", "m1"),  # 空
            self._msg("在吗", "m2"),
            self._msg("", "m3"),  # 空
        ]
        query, is_multi = scheduler._build_query(msgs)
        assert query == "在吗"
        assert is_multi is False

    def test_build_query_all_empty_returns_empty(self):
        """全空消息 → 返空字符串（让 _handle_fire 走正常流程）"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        msgs = [self._msg(""), self._msg("   ")]
        query, is_multi = scheduler._build_query(msgs)
        assert query == ""
        assert is_multi is False

    def test_is_refund_intent_any_message(self):
        """退款意图：任一消息含退款关键词 → 命中"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        msgs = [
            self._msg("在吗"),
            self._msg("这个充电宝多大容量"),
            self._msg("我要退款"),  # 第 3 条有退款
        ]
        assert scheduler._is_refund_intent_any(msgs) is True
        # 退款在最前
        msgs2 = [self._msg("我要退款"), self._msg("在吗")]
        assert scheduler._is_refund_intent_any(msgs2) is True
        # 都没退款
        msgs3 = [self._msg("在吗"), self._msg("多大容量")]
        assert scheduler._is_refund_intent_any(msgs3) is False

    def test_dedup_any_message_seen(self):
        """去重：任一消息已 seen → 整批跳过"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        scheduler._seen_message_ids.add("m2")  # 第 2 条已见
        msgs = [
            self._msg("在吗", "m1"),
            self._msg("多大", "m2"),
            self._msg("便宜点", "m3"),
        ]
        assert scheduler._is_duplicate_any(msgs) is True
        # 全新
        msgs2 = [
            self._msg("在吗", "m_new1"),
            self._msg("多大", "m_new2"),
        ]
        assert scheduler._is_duplicate_any(msgs2) is False
        # 无 message_id 不去重（保守行为）
        msgs3 = [self._msg("在吗"), self._msg("多大")]
        assert scheduler._is_duplicate_any(msgs3) is False

    def test_mark_seen_any_message_marks_all(self):
        """mark_seen：多条消息全部 mark"""
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        msgs = [
            self._msg("在吗", "m1"),
            self._msg("多大", "m2"),
            self._msg("便宜", "m3"),
        ]
        scheduler._mark_seen_any(msgs)
        assert "m1" in scheduler._seen_message_ids
        assert "m2" in scheduler._seen_message_ids
        assert "m3" in scheduler._seen_message_ids