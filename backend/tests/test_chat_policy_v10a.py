"""V10-A: chat.py policy_query 路径 order_no 归属校验单测

修复 M14-0096：
- query "订单 ORD20260718001 退货运费谁出？" 被 IntentService 识别为 policy_query
- 原 policy_query 路径不走 OrderContextResolver → 不校验 order_no 归属
- V10-A：在 chat.py IntentService.classify 后、policy_query cache_hit 前
  加 OrderTool.get_order_by_no 校验 → 不属于当前 user 时返回 SSE not_found 流

覆盖：
1. policy_query + order_no + NOT owned → SSE not_found 流（meta.intent="not_found" + 中文提示）
2. policy_query + order_no + owned → 不触发 NOT_FOUND 分支（继续正常 policy_query）
3. policy_query + 无 order_no → 跳过校验
4. ANONYMOUS_USER_ID → 跳过校验（与 P2-4 Resolver 一致）
5. refund_query + order_no → 跳过校验（refund_query 已走 Resolver 内部校验）
6. OrderTool 抛异常 → logger.warning 放行，不阻塞正常 policy_query
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# 让模块能找到 app 包（与项目其他测试一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")


async def _consume_sse_events(generator):
    """消费 async generator，收集所有 SSE 事件 dict"""
    events = []
    async for event in generator:
        events.append(event)
    return events


def _parse_sse_events(sse_lines: list) -> list:
    """从 SSE 字符串列表解析出事件 dict（chat.py _sse_format 格式）"""
    import json
    events = []
    for line in sse_lines:
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:].strip()))
            except Exception:
                pass
    return events


async def _drain_chat_response(chat_coro):
    """等待 chat() 返回 StreamingResponse，并消费 SSE body。"""
    response = await chat_coro
    out = []
    async for item in response.body_iterator:
        out.append(item.decode("utf-8") if isinstance(item, bytes) else item)
    return out


# =============================================================
# V10-A 核心行为测试（端到端 mock）
# =============================================================

class TestChatPolicyQueryOwnershipV10A:
    """V10-A：chat.py policy_query + order_no 归属校验"""

    def _setup_mocks(self, intent="policy_query", order_no="ORD20260718001", owned_order=None):
        """构造 chat() 调用所需的全部 mock

        Returns:
            tuple: (mock_intent_classify, mock_order_tool, mock_cached_answer_fn, mock_escalation, mock_synthesizer)
        """
        mock_intent_classify = MagicMock(return_value={
            "intent": intent,
            "entities": {"order_no": order_no, "sku": None, "keywords": []},
        })
        mock_order_tool = MagicMock()
        mock_order_tool.get_order_by_no.return_value = owned_order
        mock_cached_answer = MagicMock(return_value="缓存答案")
        mock_escalation = MagicMock()
        mock_escalation.handoff.return_value = MagicMock(
            handoff_id="H00000000", reason_label="", to_dict=lambda: {}
        )
        mock_synthesizer = MagicMock()
        mock_synthesizer.run_stream.return_value = iter([
            ("meta", {
                "intent": intent,
                "entities": {"order_no": order_no, "sku": None, "keywords": []},
                "contexts": [],
                "scores": [],
            }),
        ])
        return (mock_intent_classify, mock_order_tool, mock_cached_answer, mock_escalation, mock_synthesizer)

    def _make_user(self, user_id=10009):
        """构造 mock User 对象（chat.py 接受 Optional[User]）"""
        user = MagicMock()
        user.id = user_id
        user.username = f"user_{user_id}"
        return user

    def _run_chat(
        self, chat, request, payload, user,
        mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth,
    ):
        """用确定性边界 mock 执行 chat() 并消费 StreamingResponse。"""
        import asyncio

        guard_allowed = MagicMock(allowed=True)
        with patch("app.api.chat.load_history_with_fallback", return_value=[]), \
             patch("app.api.chat.behavior_monitor.record_request"), \
             patch("app.api.chat.input_guard.check", return_value=guard_allowed), \
             patch("app.api.chat.detect_p0_escalate", return_value=None), \
             patch("app.api.chat.detect_handoff_keyword", return_value=False), \
             patch("app.api.chat.IntentService.classify", mock_intent), \
             patch("app.api.chat.OrderTool", mock_order_tool), \
             patch("app.api.chat.get_cached_answer", mock_cached), \
             patch("app.api.chat.get_escalation_service", return_value=mock_escalation), \
             patch("app.api.chat.try_log_action"), \
             patch("app.services.chat.orchestrator.Synthesizer.run_stream", mock_synth.run_stream):
            return asyncio.run(_drain_chat_response(chat(request, payload, user)))

    def test_policy_query_order_no_not_owned_returns_not_found(self):
        """policy_query + order_no 不属于当前 user → SSE not_found 流（M14-0096 主场景）"""
        from app.api.chat import chat
        from app.schemas.chat import ChatRequest

        (mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth) = self._setup_mocks(
            intent="policy_query", order_no="ORD20260718001", owned_order=None,
        )
        user = self._make_user(user_id=10009)

        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        request.client.host = "127.0.0.1"
        request.headers = {"x-forwarded-for": "", "user-agent": "test"}

        payload = ChatRequest(query="订单 ORD20260718001 退货运费谁出？")

        events = self._run_chat(
            chat, request, payload, user,
            mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth,
        )

        sse_events = _parse_sse_events(events)
        # 第一个 SSE meta 事件应为 not_found
        meta_events = [e for e in sse_events if e.get("type") == "meta"]
        assert len(meta_events) >= 1, f"应至少 1 条 meta 事件，实际 {len(meta_events)}"
        first_meta = meta_events[0]
        assert first_meta["intent"] == "not_found", f"应 not_found，实际 {first_meta['intent']}"
        assert first_meta["resolver_action"] == "not_found"
        assert first_meta["resolver_reason"] == "policy_query_order_not_owned_v10_a"
        # 应含中文提示 token
        token_texts = "".join(e.get("text", "") for e in sse_events if e.get("type") == "token")
        assert "ORD20260718001" in token_texts, f"提示应含 order_no，实际: {token_texts}"
        assert "未找到" in token_texts or "不存在" in token_texts or "不正确" in token_texts
        # OrderTool.get_order_by_no 应被调用过
        mock_order_tool.get_order_by_no.assert_called_once_with(10009, "ORD20260718001")

    def test_policy_query_order_no_owned_continues_normal_flow(self):
        """policy_query + order_no 属于当前 user → 校验通过，继续走正常 policy_query"""
        from app.api.chat import chat
        from app.schemas.chat import ChatRequest

        owned = {"order_no": "ORD20260718001", "status": "shipped", "total_amount": 99.0}
        (mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth) = self._setup_mocks(
            intent="policy_query", order_no="ORD20260718001", owned_order=owned,
        )
        user = self._make_user(user_id=10009)

        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        request.client.host = "127.0.0.1"
        request.headers = {"x-forwarded-for": "", "user-agent": "test"}

        payload = ChatRequest(query="订单 ORD20260718001 退货运费谁出？")

        events = self._run_chat(
            chat, request, payload, user,
            mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth,
        )

        sse_events = _parse_sse_events(events)
        meta_events = [e for e in sse_events if e.get("type") == "meta"]
        # 第一个 meta 应是 policy_query（不是 not_found）
        assert meta_events[0]["intent"] == "policy_query", \
            f"应 policy_query 继续走，实际: {meta_events[0]['intent']}"
        # OrderTool.get_order_by_no 应被调用过（且返回 owned）
        mock_order_tool.get_order_by_no.assert_called_once_with(10009, "ORD20260718001")

    def test_policy_query_without_order_no_skips_ownership_check(self):
        """policy_query 无 order_no → 跳过归属校验。"""
        from app.api.chat import chat
        from app.schemas.chat import ChatRequest

        (mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth) = self._setup_mocks(
            intent="policy_query", order_no=None, owned_order=None,
        )
        user = self._make_user(user_id=10009)
        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        request.client.host = "127.0.0.1"
        request.headers = {"x-forwarded-for": "", "user-agent": "test"}
        payload = ChatRequest(query="退货运费谁出？")

        events = self._run_chat(
            chat, request, payload, user,
            mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth,
        )

        sse_events = _parse_sse_events(events)
        meta_events = [e for e in sse_events if e.get("type") == "meta"]
        assert meta_events[0]["intent"] == "policy_query"
        mock_order_tool.get_order_by_no.assert_not_called()

    def test_anonymous_user_skips_ownership_check(self):
        """ANONYMOUS_USER_ID → 跳过校验（与 P2-4 Resolver 一致，避免账号枚举）"""
        from app.api.chat import chat
        from app.schemas.chat import ChatRequest

        (mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth) = self._setup_mocks(
            intent="policy_query", order_no="ORD20260718001", owned_order=None,
        )
        user = self._make_user(user_id=0)  # ANONYMOUS_USER_ID = 0

        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        request.client.host = "127.0.0.1"
        request.headers = {"x-forwarded-for": "", "user-agent": "test"}

        payload = ChatRequest(query="订单 ORD20260718001 退货运费谁出？")

        events = self._run_chat(
            chat, request, payload, user,
            mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth,
        )

        # ANONYMOUS 时不应调用 OrderTool.get_order_by_no
        mock_order_tool.get_order_by_no.assert_not_called()

    def test_refund_query_skips_ownership_check(self):
        """refund_query → 跳过 V10-A 校验（refund_query 已走 OrderContextResolver 内部校验）"""
        from app.api.chat import chat
        from app.schemas.chat import ChatRequest

        (mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth) = self._setup_mocks(
            intent="refund_query", order_no="ORD20260718001", owned_order=None,
        )
        user = self._make_user(user_id=10009)

        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        request.client.host = "127.0.0.1"
        request.headers = {"x-forwarded-for": "", "user-agent": "test"}

        payload = ChatRequest(query="订单 ORD20260718001 怎么退款")

        events = self._run_chat(
            chat, request, payload, user,
            mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth,
        )

        # refund_query 不应触发 V10-A 校验
        mock_order_tool.get_order_by_no.assert_not_called()

    def test_order_tool_exception_does_not_block(self):
        """OrderTool 抛异常 → logger.warning 放行，继续正常 policy_query"""
        from app.api.chat import chat
        from app.schemas.chat import ChatRequest

        mock_intent = MagicMock(return_value={
            "intent": "policy_query",
            "entities": {"order_no": "ORD20260718001", "sku": None, "keywords": []},
        })
        mock_order_tool = MagicMock()
        mock_order_tool.get_order_by_no.side_effect = RuntimeError("DB connection lost")
        mock_cached = MagicMock(return_value="缓存答案")
        mock_escalation = MagicMock()
        mock_synth = MagicMock()
        mock_synth.run_stream.return_value = iter([])

        user = self._make_user(user_id=10009)
        request = MagicMock()
        request.is_disconnected = AsyncMock(return_value=False)
        request.client.host = "127.0.0.1"
        request.headers = {"x-forwarded-for": "", "user-agent": "test"}
        payload = ChatRequest(query="订单 ORD20260718001 退货运费谁出？")

        events = self._run_chat(
            chat, request, payload, user,
            mock_intent, mock_order_tool, mock_cached, mock_escalation, mock_synth,
        )

        # OrderTool 异常时不应返回 not_found（继续正常 policy_query）
        sse_events = _parse_sse_events(events)
        meta_events = [e for e in sse_events if e.get("type") == "meta"]
        assert len(meta_events) > 0
        # 第一个 meta 不应是 not_found（异常时放行）
        assert meta_events[0].get("intent") != "not_found", \
            f"OrderTool 异常时应放行，不应 not_found，实际: {meta_events[0]}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])