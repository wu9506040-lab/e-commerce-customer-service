"""
BuyerProfile — 买家画像拉取/缓存（V16.1）

职责：
1. 给定买家 unb → 拉昵称/历史会话/最近消息/是否老客
2. LRU + TTL 缓存（避免对同一买家重复拉取）
3. MCP 不可用时降级（返空 profile + 标记 fetch_error）
4. 暴露 to_prompt_context() 给 LLM

调用方：
  ReplyScheduler.on_fire(cid, messages)
    → BuyerProfile.get_profile(unb)
    → profile.to_prompt_context()
    → 智能客服 /api/chat (作为 system prompt 的 context_block 注入)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BuyerProfileData:
    """买家画像数据（dataclass 便于 to_prompt_context）"""

    unb: str
    nick: str = ""
    history_with_me: list[dict] = field(default_factory=list)
    last_message: str = ""
    is_returning_buyer: bool = False
    fetch_error: str = ""  # MCP 拉失败时填错误码
    fetched_at: float = 0.0

    def to_prompt_context(self) -> str:
        """生成给 LLM 的 context 块（人类可读 + 简洁）

        设计：直接拼字符串，避免引入 Jinja2 等模板。
        """
        if self.fetch_error:
            return f"【闲鱼买家】unb={self.unb}（信息拉取失败：{self.fetch_error}）"

        lines = [
            f"【闲鱼买家】昵称={self.nick or '?'} unb={self.unb}",
        ]
        if self.is_returning_buyer:
            lines.append("身份：老客（之前有订单相关会话）")
        else:
            lines.append("身份：新客（无历史）")
        if self.history_with_me:
            history_summary = ", ".join(
                f"{h.get('last_msg', '?')[:30]!r}" for h in self.history_with_me[:3]
            )
            lines.append(f"本店历史会话({len(self.history_with_me)}条)：{history_summary}")
        if self.last_message:
            lines.append(f"最新消息：{self.last_message[:100]}")
        return "\n".join(lines)


class BuyerProfile:
    """买家画像拉取 + 缓存

    设计取舍（V16.1）：
    - 简单 dict 缓存（非 LRU；按需可换 functools.lru_cache）
    - TTL 默认 300s（5 分钟）—— 同一买家 5 分钟内不重拉
    - MCP 错误降级：返空 profile，标记 fetch_error
    - is_enabled=False 短路（不调 MCP）
    """

    def __init__(
        self,
        client=None,  # 默认用 get_xianyu_client
        cache_ttl: float = 300.0,
        list_conversations_limit: int = 50,
    ):
        if client is None:
            from app.integrations import get_xianyu_client
            client = get_xianyu_client()
        self._client = client
        self._cache_ttl = cache_ttl
        self._list_limit = list_conversations_limit
        self._cache: dict[str, BuyerProfileData] = {}

    def get_profile(self, unb: str) -> BuyerProfileData:
        """拉取买家画像（缓存命中返 cache）"""
        cached = self._cache.get(unb)
        if cached is not None and (time.time() - cached.fetched_at) < self._cache_ttl:
            return cached

        if not self._client.is_enabled():
            return self._put_empty(unb, fetch_error="xianyu_tools_disabled")

        # 1. 列所有会话（找同 unb 的历史）
        result = self._client.list_conversations(limit=self._list_limit)
        if "error" in result:
            return self._put_empty(unb, fetch_error=result["error"])

        sessions = (
            result.get("data", {}).get("sessions", [])
            if isinstance(result.get("data"), dict)
            else []
        )
        # 过滤同 unb
        same_unb_sessions = [
            s for s in sessions
            if str(s.get("peer_user_id", "")) == str(unb)
        ]
        # 排序（按 ts 倒序，最新在前）
        same_unb_sessions.sort(key=lambda s: s.get("ts", 0), reverse=True)

        if not same_unb_sessions:
            return self._put(unb, BuyerProfileData(unb=unb, fetched_at=time.time()))

        # 2. 拉最新会话的最近消息
        latest_session = same_unb_sessions[0]
        latest_cid = latest_session.get("session_id") or latest_session.get("cid")
        last_message = ""
        if latest_cid:
            hist = self._client.get_conversation_history(cid=latest_cid, pages=1)
            if "error" not in hist:
                msgs = hist.get("data", []) if isinstance(hist.get("data"), list) else []
                # 取最近一条买家消息
                for m in reversed(msgs):
                    if str(m.get("send_user_id", "")) == str(unb):
                        text = (
                            m.get("message", {}).get("text", {}).get("text", "")
                            if isinstance(m.get("message"), dict)
                            else ""
                        )
                        if text:
                            last_message = text
                            break

        nick = latest_session.get("peer_nick", "")
        is_returning = any(
            s.get("item_id") for s in same_unb_sessions
        )
        profile = BuyerProfileData(
            unb=unb,
            nick=nick,
            history_with_me=same_unb_sessions,
            last_message=last_message,
            is_returning_buyer=is_returning,
            fetched_at=time.time(),
        )
        return self._put(unb, profile)

    def invalidate(self, unb: str) -> None:
        """强制下次重拉"""
        self._cache.pop(unb, None)

    # =============================================================
    # 内部
    # =============================================================
    def _put(self, unb: str, profile: BuyerProfileData) -> BuyerProfileData:
        self._cache[unb] = profile
        return profile

    def _put_empty(self, unb: str, fetch_error: str) -> BuyerProfileData:
        profile = BuyerProfileData(
            unb=unb,
            fetch_error=fetch_error,
            fetched_at=time.time(),
        )
        self._cache[unb] = profile
        return profile


__all__ = ["BuyerProfile", "BuyerProfileData"]