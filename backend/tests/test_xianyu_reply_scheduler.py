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
        """首次延迟在 [first_min_delay, first_max_delay] 区间内（V16.8 P1-D: 默认 0-3s）

        旧(V16.3.1): 首次 10-15s — 反功能（IM 真人客服响应节奏 < 3s）
        现(V16.8 P1-D): 首次/后续都 0-3s
        """
        scheduler = ReplyScheduler(
            xianyu_client=MagicMock(),
            chat_api_url="http://test",
        )
        # 强制 first_delays 为窄区间便于断言
        scheduler._first_min_delay = 0.05
        scheduler._first_max_delay = 0.1
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
        """V16.8 P1-B: send_message 永远失败 → 永远重试，daemon 不死就不放弃

        旧(V16.6): 第 N 次放弃 → result["error"] 返回
        现(V16.8 P1-B): 无限重试 + 5s→60s 指数退避 → 测试必须 mock 最终成功才能 return
        """
        mock_client = MagicMock()
        # 第一次失败，第二次成功（永远重试直到成功是 V16.8 新语义）
        mock_client.send_message.side_effect = [
            {"error": "mcp_call_timeout"},
            {"ok": True, "message_id": "M1"},
        ]
        scheduler = ReplyScheduler(
            xianyu_client=mock_client,
            chat_api_url="http://test",
            max_retries=1,
        )
        with patch("time.sleep"):  # mock sleep 不真等
            result = scheduler._send_with_retry(
                cid="c1", toid="u1", text="hi"
            )
        assert result.get("ok") is True
        assert result["message_id"] == "M1"
        assert mock_client.send_message.call_count == 2

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
        """V16.8 P1-B: 限流永远重试（99999 配额 = 实际无限），不再 6 次后放弃"""
        mock_client = MagicMock()
        # 永远返限流（测试用 break 跳出 while True）
        rate_limit_count = 0
        def fake_send(cid, toid, text):
            nonlocal rate_limit_count
            rate_limit_count += 1
            return {
                "ok": False,
                "error_type": "RateLimitedError",
                "message": "限流：再等 0.1s",
            }
        mock_client.send_message.side_effect = fake_send

        scheduler = ReplyScheduler(
            xianyu_client=mock_client,
            chat_api_url="http://test",
            max_retries=0,
        )
        # V16.8 P1-B: while True — 第 7 次跳出让测试不死循环
        def fake_sleep(sec):
            if rate_limit_count >= 7:
                raise StopIteration("test_exit")
        with patch("time.sleep", side_effect=fake_sleep):
            try:
                result = scheduler._send_with_retry(
                    cid="c1", toid="u1", text="hi"
                )
            except StopIteration:
                pass
        # 7 次限流尝试（用 sleep 模拟跳出）
        assert mock_client.send_message.call_count == 7

    def test_send_non_rate_limit_keeps_old_exponential_backoff(self):
        """V16.8 P1-B: 非限流错误指数退避 5s/10s/20s/40s/60s cap（无成功时永远重试）

        旧(V16.6): 1s/2s 退避，3 次后放弃
        现(V16.8 P1-B): 5s/10s/20s/40s/60s cap 退避，永远重试
        """
        mock_client = MagicMock()
        # 永远失败 — 但 mock sleep + 测 sleep 序列
        mock_client.send_message.return_value = {
            "ok": False,
            "error_type": "NetworkError",
            "message": "timeout",
        }
        scheduler = ReplyScheduler(
            xianyu_client=mock_client,
            chat_api_url="http://test",
            max_retries=2,
        )
        # V16.8 P1-B: while True 永远循环 — 我们用死循环计数器（只测前 5 次就 break）
        non_rl_count = 0
        def fake_send(cid, toid, text):
            nonlocal non_rl_count
            non_rl_count += 1
            return {"ok": False, "error_type": "NetworkError", "message": "timeout"}
        mock_client.send_message.side_effect = fake_send

        sleeps = []
        def fake_sleep(sec):
            sleeps.append(sec)
            # V16.8 P1-B: 5 次重试后让出（mock 让测试不真等）
            if len(sleeps) >= 5:
                raise StopIteration("test_exit")
        with patch("time.sleep", side_effect=fake_sleep):
            try:
                scheduler._send_with_retry(cid="c1", toid="u1", text="hi")
            except StopIteration:
                pass
        # 5 次尝试，5 次 sleep（5s/10s/20s/40s/60s）— 第 5 次 sleep 时 raise 但已记录
        assert mock_client.send_message.call_count == 5
        assert sleeps == [5.0, 10.0, 20.0, 40.0, 60.0]


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