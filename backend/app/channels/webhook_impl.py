"""
WebhookAdapter 默认实现（Sprint 14 · CLAUDE.md §9.9 落地）

设计要点：
- HMAC-SHA256 签名校验（X-Signature header），secret 从 settings 读取
  - 测试契约：缺失 / 错误 secret → InvalidSignatureError
- 发送走 httpx.AsyncClient POST（可注入 mock，便于单测）
- 限流（HTTP 429）映射为 RateLimitError；其他非 2xx → SendMessageError
- get_user_info 走 GET {base_url}/users/{user_id}
"""
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.channels.protocols import (
    ChannelAdapter,
    ChannelAdapterFactory,
    InvalidSignatureError,
    RateLimitError,
    SendMessageError,
    UnsupportedMessageTypeError,
)
from app.schemas.channel_event import ChannelEvent, ChannelReply

logger = logging.getLogger(__name__)


# 仅支持 text；image / file / event 留 V3+ YAGNI
_SUPPORTED_MESSAGE_TYPES = {"text"}


class WebhookAdapter(ChannelAdapter):
    """Webhook 默认实现 — 适配任意支持 webhook 回调的外部系统"""

    channel_type = "webhook"

    def __init__(
        self,
        secret: str = "",
        base_url: str = "",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """
        Args:
            secret: HMAC-SHA256 签名密钥；空字符串表示关闭签名校验（仅 dev/test）
            base_url: 接入方回调根 URL（如 https://shop.example.com/api/ai-callback）
                      为空时 send() 抛 SendMessageError（无可用 endpoint）
            client: 可注入 httpx.AsyncClient（测试用）；不传则懒创建
        """
        self._secret = secret
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._client = client
        self._owns_client = client is None  # 用于 close() 决定是否释放

    # =============================================================
    # 内部工具
    # =============================================================
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """释放自建 client（注入的 client 由调用方负责）"""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    def _verify_signature(self, body: bytes, signature: Optional[str]) -> None:
        """
        校验 HMAC-SHA256 签名。
        - secret 为空：跳过校验（dev/test 用，生产必须配置）
        - signature header 缺失或 mismatch：抛 InvalidSignatureError
        """
        if not self._secret:
            return  # 关闭签名校验
        if not signature:
            raise InvalidSignatureError("missing X-Signature header")
        # 接入方可选 base64 或 hex；spec 不强制，先支持 hex（MVP）
        expected = hmac.new(
            self._secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise InvalidSignatureError("signature mismatch")

    # =============================================================
    # ChannelAdapter 实现
    # =============================================================
    async def receive(self, payload: dict, headers: dict) -> ChannelEvent:
        """
        解析 webhook payload 为标准 ChannelEvent。

        约定字段：
        - user_id: 接入方用户 ID（必填）
        - session_id: 接入方会话 ID（必填；缺省用 user_id）
        - message: 消息文本（必填）
        - message_type: 默认 text
        - timestamp: ISO8601 字符串（缺省取当前 UTC）
        - metadata: dict（透传）

        签名校验：仅当启用 secret 时执行；headers 应含 X-Signature（hex-encoded HMAC-SHA256）。
        注：payload 字典的 JSON 序列化需与接入方一致；通常用紧凑格式（无空格），
        接入方实现时建议固定 json.dumps(payload, separators=(",", ":")) 后再 HMAC。
        """
        # 0. 签名校验（如启用）
        if self._secret:
            signature = headers.get("X-Signature") or headers.get("x-signature")
            if not signature:
                raise InvalidSignatureError("missing X-Signature header")
            import json as _json
            body_bytes = _json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self._verify_signature(body_bytes, signature)

        # 1. 必填字段校验
        user_id = payload.get("user_id")
        message = payload.get("message")
        if not user_id or not message:
            raise InvalidSignatureError(
                "payload missing required fields (user_id, message)",
            )

        # 2. message_type 校验
        msg_type = payload.get("message_type", "text")
        if msg_type not in _SUPPORTED_MESSAGE_TYPES:
            raise UnsupportedMessageTypeError(
                f"unsupported message_type: {msg_type}",
            )

        # 3. 时间戳解析
        ts_raw = payload.get("timestamp")
        if isinstance(ts_raw, str):
            try:
                # 处理 "Z" 后缀（Python <3.11 兼容性兜底）
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        return ChannelEvent(
            channel_type=self.channel_type,
            channel_user_id=str(user_id),
            channel_session_id=str(payload.get("session_id") or user_id),
            message=str(message),
            message_type=msg_type,
            metadata=payload.get("metadata") or {},
            timestamp=ts,
        )

    async def send(self, event: ChannelEvent, reply: ChannelReply) -> dict:
        """
        把 AI 回复 POST 到接入方 callback URL。
        - callback URL = base_url + metadata["callback_url"]（每个事件可覆盖）
        - 无 base_url 且无 callback_url：抛 SendMessageError
        """
        callback = (event.metadata or {}).get("callback_url") or self._base_url
        if not callback:
            raise SendMessageError(
                "no callback URL configured (set base_url or event.metadata.callback_url)",
            )

        body = reply.model_dump()
        # 把原始事件 context 回传，便于接入方关联 conversation
        body["_context"] = {
            "channel_type": event.channel_type,
            "channel_user_id": event.channel_user_id,
            "channel_session_id": event.channel_session_id,
        }

        client = self._get_client()
        try:
            resp = await client.post(callback, json=body)
        except httpx.HTTPError as e:
            raise SendMessageError(f"network error: {e}") from e

        if resp.status_code == 429:
            raise RateLimitError(f"upstream rate limit (HTTP 429)")
        if resp.status_code >= 400:
            raise SendMessageError(
                f"upstream returned {resp.status_code}: {resp.text[:200]}",
            )

        # 尝试 JSON 解析；非 JSON 也接受（如 204 No Content）
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code, "body": resp.text}

    async def get_user_info(self, user_id: str) -> dict:
        """
        GET {base_url}/users/{user_id} → 接入方返回用户信息 dict
        无 base_url 时抛 SendMessageError
        """
        if not self._base_url:
            raise SendMessageError(
                "no base_url configured for get_user_info",
            )

        url = f"{self._base_url}/users/{user_id}"
        client = self._get_client()
        try:
            resp = await client.get(url)
        except httpx.HTTPError as e:
            raise SendMessageError(f"network error: {e}") from e

        if resp.status_code == 429:
            raise RateLimitError("upstream rate limit (HTTP 429)")
        if resp.status_code >= 400:
            raise SendMessageError(
                f"upstream returned {resp.status_code}: {resp.text[:200]}",
            )

        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}


class WebhookAdapterFactory(ChannelAdapterFactory):
    """WebhookAdapter 单实例工厂 — FastAPI Depends 注入"""

    def __init__(
        self,
        secret: str = "",
        base_url: str = "",
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._adapter = WebhookAdapter(secret=secret, base_url=base_url, client=client)

    def get(self, channel_type: str) -> ChannelAdapter:
        """MVP: 仅支持 webhook；其他 channel_type 抛 ValueError（YAGNI 不预实现）"""
        if channel_type != "webhook":
            raise ValueError(
                f"unsupported channel_type: {channel_type} (only 'webhook' in MVP)",
            )
        return self._adapter
