"""
BuyerProfile 测试 — 拉取/缓存买家画像

按 CLAUDE.md 调试铁律：测试 mock XianyuMCPClient（不真连 MCP）
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.xianyu.buyer_profile import BuyerProfile, BuyerProfileData


# =============================================================
# 1. 拉取逻辑
# =============================================================
class TestFetch:
    def test_first_call_fetches_from_mcp(self):
        """首次拉取 → 调 list_conversations + message history"""
        mock_client = MagicMock()
        mock_client.list_conversations.return_value = {
            "ok": True,
            "data": {"sessions": [
                {"session_id": "c1", "peer_nick": "zwyyy", "peer_user_id": "u123",
                 "last_msg": "老客在吗", "ts": 1234567890, "item_id": ""},
                {"session_id": "c2", "peer_nick": "zwyyy", "peer_user_id": "u123",
                 "last_msg": "上次买的不错", "ts": 1234567800, "item_id": "ITEM1"},
            ]}
        }
        mock_client.get_conversation_history.return_value = {
            "ok": True,
            "data": [{"message_id": "m1", "send_user_id": "u123",
                      "send_user_name": "zwyyy",
                      "message": {"text": {"text": "在吗"}}}]}
        mock_client.is_enabled.return_value = True

        p = BuyerProfile(client=mock_client)
        profile = p.get_profile("u123")

        assert profile.unb == "u123"
        assert profile.nick == "zwyyy"
        assert profile.is_returning_buyer is True  # 有 ITEM1 订单相关
        assert len(profile.history_with_me) == 2
        assert profile.last_message == "在吗"
        mock_client.list_conversations.assert_called_once_with(limit=50)
        mock_client.get_conversation_history.assert_called_once_with(cid="c1", pages=1)

    def test_new_buyer_no_history(self):
        """新买家：list_conversations 不含该 unb → 0 历史 + is_returning=False"""
        mock_client = MagicMock()
        mock_client.list_conversations.return_value = {
            "ok": True, "data": {"sessions": []}
        }
        mock_client.is_enabled.return_value = True

        p = BuyerProfile(client=mock_client)
        profile = p.get_profile("u999")
        assert profile.nick == ""
        assert profile.history_with_me == []
        assert profile.is_returning_buyer is False
        assert profile.last_message == ""

    def test_is_returning_buyer_when_has_item_id(self):
        """至少一个会话 item_id 非空 → 老客（买过）"""
        mock_client = MagicMock()
        mock_client.list_conversations.return_value = {
            "ok": True, "data": {"sessions": [
                {"session_id": "c1", "peer_nick": "n1", "peer_user_id": "u1",
                 "last_msg": "x", "ts": 1, "item_id": "ITEM99"},
            ]}
        }
        mock_client.get_conversation_history.return_value = {
            "ok": True, "data": []
        }
        mock_client.is_enabled.return_value = True
        p = BuyerProfile(client=mock_client)
        profile = p.get_profile("u1")
        assert profile.is_returning_buyer is True


# =============================================================
# 2. 缓存
# =============================================================
class TestCache:
    def test_second_call_uses_cache(self):
        """30s 缓存期内不重复拉"""
        mock_client = MagicMock()
        mock_client.list_conversations.return_value = {
            "ok": True, "data": {"sessions": []}
        }
        mock_client.is_enabled.return_value = True
        p = BuyerProfile(client=mock_client, cache_ttl=10.0)
        p.get_profile("u1")
        p.get_profile("u1")
        p.get_profile("u1")
        assert mock_client.list_conversations.call_count == 1

    def test_cache_expires_after_ttl(self):
        """TTL 过期后重新拉"""
        mock_client = MagicMock()
        mock_client.list_conversations.return_value = {
            "ok": True, "data": {"sessions": []}
        }
        mock_client.is_enabled.return_value = True
        p = BuyerProfile(client=mock_client, cache_ttl=0.05)
        p.get_profile("u1")
        time.sleep(0.1)
        p.get_profile("u1")
        assert mock_client.list_conversations.call_count == 2

    def test_invalidate(self):
        """invalidate 强制下次重拉"""
        mock_client = MagicMock()
        mock_client.list_conversations.return_value = {
            "ok": True, "data": {"sessions": []}
        }
        mock_client.is_enabled.return_value = True
        p = BuyerProfile(client=mock_client)
        p.get_profile("u1")
        p.invalidate("u1")
        p.get_profile("u1")
        assert mock_client.list_conversations.call_count == 2


# =============================================================
# 3. MCP 不可用降级
# =============================================================
class TestGracefulDegradation:
    def test_mcp_error_returns_empty_profile(self):
        """list_conversations 返 error → 不抛，返空 profile"""
        mock_client = MagicMock()
        mock_client.list_conversations.return_value = {"error": "mcp_call_timeout"}
        mock_client.is_enabled.return_value = True
        p = BuyerProfile(client=mock_client)
        profile = p.get_profile("u1")
        assert profile.nick == ""
        assert profile.history_with_me == []
        assert profile.fetch_error == "mcp_call_timeout"

    def test_disabled_client_returns_empty(self):
        """is_enabled=False → 返空 profile（不调 MCP）"""
        mock_client = MagicMock()
        mock_client.is_enabled.return_value = False
        p = BuyerProfile(client=mock_client)
        profile = p.get_profile("u1")
        assert profile.nick == ""
        mock_client.list_conversations.assert_not_called()


# =============================================================
# 4. to_prompt_context（喂给 LLM）
# =============================================================
class TestPromptContext:
    def test_to_prompt_context_includes_nick_and_history(self):
        """to_prompt_context 返字符串，LLM 可读"""
        mock_client = MagicMock()
        mock_client.list_conversations.return_value = {
            "ok": True, "data": {"sessions": [
                {"session_id": "c1", "peer_nick": "zwyyy", "peer_user_id": "u1",
                 "last_msg": "上次聊过", "ts": 1, "item_id": "ITEM1"},
            ]}
        }
        mock_client.get_conversation_history.return_value = {
            "ok": True, "data": [{"message_id": "m1", "send_user_id": "u1",
                      "send_user_name": "zwyyy",
                      "message": {"text": {"text": "在吗"}}}]}
        mock_client.is_enabled.return_value = True
        p = BuyerProfile(client=mock_client)
        profile = p.get_profile("u1")
        ctx = profile.to_prompt_context()
        assert "zwyyy" in ctx
        assert "在吗" in ctx
        assert "老客" in ctx or "returning" in ctx.lower() or "ITEM1" in ctx