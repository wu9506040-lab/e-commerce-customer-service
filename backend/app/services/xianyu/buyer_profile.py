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
import threading
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


class MtopProfileClient:
    """V18 P3：画像数据源 mtop 化适配器（替代已弃用的 goofish MCP client）

    把 XianyuApis 的两个 mtop 接口（probe_mtop_poller.py 验证过）适配成
    BuyerProfile 期望的旧 MCP client 形状：

      - list_conversations(limit) → mtop.taobao.idlemessage.pc.session.sync v3.0
        映射 data.sessions[].session.{sessionId,userInfo} →
             {peer_user_id, peer_nick, session_id, ts, item_id}
      - get_conversation_history(cid, pages) → mtop.idle.message.individualpage.getchatdata
        映射 data.userMessageModels → [{send_user_id, message:{text:{text}}}]

    is_enabled：apis 存在且 cookie_str 非空（daemon 进程内才成立；
    backend 容器无 cookie，自动走禁用降级）
    """

    def __init__(self, apis=None):
        self._apis = apis

    def is_enabled(self) -> bool:
        try:
            return bool(self._apis and getattr(self._apis, "cookies_str", ""))
        except Exception:
            return False

    def list_conversations(self, limit: int = 20) -> dict:
        try:
            raw = self._apis.list_sessions(fetch_num=limit)
            if not raw:
                return {"error": "mtop_session_sync_failed"}
            sessions = []
            for s in (raw.get("data") or {}).get("sessions") or []:
                sess = s.get("session") or {}
                user = sess.get("userInfo") or {}
                summary = (s.get("message") or {}).get("summary") or {}
                sessions.append({
                    "session_id": sess.get("sessionId", ""),
                    "peer_user_id": str(user.get("userId", "") or ""),
                    "peer_nick": user.get("nick", "") or "",
                    "ts": summary.get("ts", 0) or 0,
                    "item_id": "",  # session.sync 不带商品 id，置空
                    # V18: 会话摘要即最新一条消息预览（getchatdata API 已失效，
                    # 用 session.sync 的 summary 顶上）
                    "last_message": str(summary.get("summary", "") or ""),
                })
            return {"data": {"sessions": sessions}}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def get_conversation_history(self, cid: str, pages: int = 1) -> dict:
        try:
            raw = self._apis.get_chat_data(cid=cid, page_size=10 * max(1, pages))
            if not raw:
                return {"error": "mtop_getchatdata_failed"}
            data = raw.get("data") or {}
            msgs = (
                data.get("userMessageModels")
                or data.get("messages")
                or data.get("messageModels")
                or []
            )
            out = []
            for m in msgs if isinstance(msgs, list) else []:
                if not isinstance(m, dict):
                    continue
                # 容错提取：不同版本字段名不一，逐个尝试
                sender = (
                    m.get("senderUserId") or m.get("senderId")
                    or m.get("sender_user_id") or ""
                )
                # 文本藏在 message.text.text / content.text / text 等
                text = ""
                content = m.get("message") or m.get("content") or {}
                if isinstance(content, dict):
                    text = (
                        content.get("text", {}).get("text", "")
                        if isinstance(content.get("text"), dict)
                        else str(content.get("text", "") or "")
                    )
                if not text:
                    text = str(m.get("text", "") or "")
                out.append({
                    "send_user_id": str(sender),
                    "message": {"text": {"text": str(text)}},
                })
            return {"data": out}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}


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
        client=None,  # V18: 默认 None = 禁用（goofish MCP 第三方工具已弃用）
        cache_ttl: float = 300.0,
        list_conversations_limit: int = 50,
    ):
        if client is None:
            # V18（2026-10-17）：goofish MCP 第三方工具弃用（用户指令）。
            # 画像数据源待用自实现 mtop 接口重建；当前默认禁用 →
            # is_enabled() False → get_profile 走空画像短路，不发起任何网络调用。
            # 回复链路不受影响（画像本就是异步可选上下文，昵称回退"朋友"）。
            class _DisabledClient:
                """禁用桩：永不发起 MCP 调用"""

                def is_enabled(self) -> bool:
                    return False

            client = _DisabledClient()
        self._client = client
        self._cache_ttl = cache_ttl
        self._list_limit = list_conversations_limit
        self._cache: dict[str, BuyerProfileData] = {}
        # V18（2026-10-17）：异步预热支持
        self._lock = threading.Lock()
        self._inflight: set[str] = set()  # 正在后台拉取的 unb

    def get_profile_nowait(self, unb: str) -> BuyerProfileData:
        """V18：非阻塞取画像 — 缓存命中直接用；未命中立刻返空画像 +
        后台线程补拉（拉完写缓存，下一轮消息就能用上）。

        背景：get_profile 同步拉会话列表+历史要 30s+（两次 mtop 调用），
        在回复链路里阻塞，买家首响被拖到 54s。回复速度优先于画像丰富度，
        画像只作为 /chat 的加分上下文，不该卡主链路。
        """
        cached = self._cache.get(unb)
        if cached is not None and (time.time() - cached.fetched_at) < self._cache_ttl:
            return cached

        # 缓存未命中：后台补拉（不阻塞），本轮先用空画像
        self.prefetch(unb)
        return BuyerProfileData(
            unb=unb, fetch_error="fetching_background", fetched_at=0.0
        )

    def prefetch(self, unb: str) -> None:
        """V18：后台线程预热画像（同 unb 去重，防重复拉取）"""
        with self._lock:
            if unb in self._inflight:
                return
            # 已有新鲜缓存也不用拉
            cached = self._cache.get(unb)
            if cached is not None and (time.time() - cached.fetched_at) < self._cache_ttl:
                return
            self._inflight.add(unb)

        def _worker():
            try:
                self.get_profile(unb)
            except Exception as e:  # 后台线程绝不抛
                logger.warning(f"BuyerProfile 后台预热失败 unb={unb}: {e}")
            finally:
                with self._lock:
                    self._inflight.discard(unb)

        threading.Thread(
            target=_worker, name=f"profile-prefetch-{unb}", daemon=True
        ).start()

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
        if not last_message:
            # V18: mtop 路径无逐条历史 API → 用 session.sync 的会话摘要兜底
            # （是最新一条消息，可能是卖家自己发的——画像上下文足够用）
            last_message = str(latest_session.get("last_message", "") or "")

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