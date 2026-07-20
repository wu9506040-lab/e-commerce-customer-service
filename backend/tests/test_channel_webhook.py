"""
Sprint 14 · ChannelAdapter Webhook 测试（spec §1.4 · 6 用例必过）

覆盖范围：
1. test_webhook_receive_text_message      标准 webhook payload → ChannelEvent 字段全对
2. test_webhook_receive_invalid_signature  错误签名 → InvalidSignatureError
3. test_webhook_send_text_reply            mock httpx → 正确 POST 到 callback URL
4. test_webhook_send_rate_limit            mock 429 → RateLimitError
5. test_webhook_get_user_info              mock GET → 返接入方用户 dict
6. test_factory_returns_webhook_adapter    factory.get("webhook") → WebhookAdapter 实例

设计原则：
- 不依赖真实外部 HTTP 服务；用 AsyncMock 注入 httpx.AsyncClient
- 单测覆盖纯逻辑 + 异常映射；端点 HTTP 测试由集成验证负责
- 沿用项目惯例：async 代码用 asyncio.run() 包裹（避免引入 pytest-asyncio）
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# 让模块能找到 app 包（沿用现有测试约定）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 触发 import 时不会因为 env 缺失报错
os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")


def _run(coro):
    """asyncio.run() 包装 — 项目惯例（避免引入 pytest-asyncio）"""
    return asyncio.run(coro)


# =============================================================
# 公共 fixture
# =============================================================
@pytest.fixture
def mock_httpx_client() -> AsyncMock:
    """Mock httpx.AsyncClient — get/post 返回可控响应"""
    client = AsyncMock()
    # 默认 post/get 成功
    default_resp = MagicMock()
    default_resp.status_code = 200
    default_resp.json.return_value = {"ok": True, "msg_id": "x1"}
    default_resp.text = '{"ok": true, "msg_id": "x1"}'
    client.post.return_value = default_resp
    client.get.return_value = default_resp
    return client


@pytest.fixture
def adapter(mock_httpx_client):
    """WebhookAdapter（secret=test-secret, 注入 mock httpx client）"""
    from app.channels.webhook_impl import WebhookAdapter

    return WebhookAdapter(
        secret="test-secret",
        base_url="https://shop.example.com/api/ai-cb",
        client=mock_httpx_client,
    )


@pytest.fixture
def adapter_no_secret(mock_httpx_client):
    """不启用签名校验的 adapter（dev/test 模式）"""
    from app.channels.webhook_impl import WebhookAdapter

    return WebhookAdapter(
        secret="",
        base_url="https://shop.example.com/api/ai-cb",
        client=mock_httpx_client,
    )


# =============================================================
# 1. test_webhook_receive_text_message
# =============================================================
def test_webhook_receive_text_message(adapter_no_secret):
    """标准 webhook payload → ChannelEvent 字段全对"""
    payload = {
        "user_id": "u-1001",
        "session_id": "s-abc",
        "message": "我的订单什么时候发货？",
        "message_type": "text",
        "timestamp": "2026-07-20T10:30:00Z",
        "metadata": {"order_no": "ORD20260720001", "shop_id": "shop-7"},
    }
    headers = {"X-Signature": "ignored-when-secret-empty"}

    event = _run(adapter_no_secret.receive(payload=payload, headers=headers))

    assert event.channel_type == "webhook"
    assert event.channel_user_id == "u-1001"
    assert event.channel_session_id == "s-abc"
    assert event.message == "我的订单什么时候发货？"
    assert event.message_type == "text"
    assert event.metadata["order_no"] == "ORD20260720001"
    # timestamp 必须被正确解析（带时区）
    assert event.timestamp.year == 2026
    assert event.timestamp.tzinfo is not None


# =============================================================
# 2. test_webhook_receive_invalid_signature
# =============================================================
def test_webhook_receive_invalid_signature(adapter):
    """错误签名 → InvalidSignatureError"""
    from app.channels.protocols import InvalidSignatureError

    payload = {"user_id": "u-1", "message": "hello"}
    headers = {"X-Signature": "deadbeef-not-matching"}

    with pytest.raises(InvalidSignatureError):
        _run(adapter.receive(payload=payload, headers=headers))


# =============================================================
# 3. test_webhook_send_text_reply
# =============================================================
def test_webhook_send_text_reply(adapter, mock_httpx_client):
    """mock httpx → 正确 POST 到 callback URL"""
    from app.schemas.channel_event import ChannelReply

    payload = {
        "user_id": "u-1",
        "session_id": "s-1",
        "message": "hi",
        "metadata": {"callback_url": "https://shop.example.com/api/cb"},
    }
    # 构造合法签名（hex HMAC-SHA256(secret, JSON body)）
    import hashlib
    import hmac
    import json as _json
    body_bytes = _json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = hmac.new(b"test-secret", body_bytes, hashlib.sha256).hexdigest()
    headers = {"X-Signature": sig}

    event = _run(adapter.receive(payload=payload, headers=headers))
    reply = ChannelReply(text="您好，客服小助手为您服务~")

    result = _run(adapter.send(event=event, reply=reply))

    # 断言：POST 到正确的 URL
    mock_httpx_client.post.assert_called_once()
    call = mock_httpx_client.post.call_args
    assert call.args[0] == "https://shop.example.com/api/cb"
    # 断言：body 含 text + _context
    body = call.kwargs["json"]
    assert body["text"] == "您好，客服小助手为您服务~"
    assert body["_context"]["channel_user_id"] == "u-1"
    # 断言：返回上游响应
    assert result["ok"] is True


# =============================================================
# 4. test_webhook_send_rate_limit
# =============================================================
def test_webhook_send_rate_limit(adapter, mock_httpx_client):
    """mock 429 → RateLimitError"""
    from app.channels.protocols import RateLimitError
    from app.schemas.channel_event import ChannelReply

    payload = {
        "user_id": "u-2",
        "message": "test",
        "metadata": {"callback_url": "https://shop.example.com/api/cb"},
    }
    import hashlib
    import hmac
    import json as _json
    body_bytes = _json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = hmac.new(b"test-secret", body_bytes, hashlib.sha256).hexdigest()
    headers = {"X-Signature": sig}

    # 改 mock：post 返回 429
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.text = "rate limited"
    mock_httpx_client.post.return_value = resp_429

    event = _run(adapter.receive(payload=payload, headers=headers))
    reply = ChannelReply(text="reply")

    with pytest.raises(RateLimitError):
        _run(adapter.send(event=event, reply=reply))


# =============================================================
# 5. test_webhook_get_user_info
# =============================================================
def test_webhook_get_user_info(adapter, mock_httpx_client):
    """mock GET → 返接入方用户 dict"""
    # 改 mock：get 返回用户信息
    user_resp = MagicMock()
    user_resp.status_code = 200
    user_resp.json.return_value = {
        "nickname": "张三",
        "avatar": "https://cdn.example.com/u/1.png",
        "level": "gold",
    }
    mock_httpx_client.get.return_value = user_resp

    user_info = _run(adapter.get_user_info("u-1001"))

    # 断言：GET 到正确 URL
    mock_httpx_client.get.assert_called_once()
    assert mock_httpx_client.get.call_args.args[0] == (
        "https://shop.example.com/api/ai-cb/users/u-1001"
    )
    # 断言：返回接入方用户 dict
    assert user_info["nickname"] == "张三"
    assert user_info["level"] == "gold"


# =============================================================
# 6. test_factory_returns_webhook_adapter
# =============================================================
def test_factory_returns_webhook_adapter():
    """factory.get('webhook') → WebhookAdapter 实例"""
    from app.channels.protocols import ChannelAdapter
    from app.channels.webhook_impl import WebhookAdapter, WebhookAdapterFactory

    factory = WebhookAdapterFactory(secret="s", base_url="https://x.example.com")
    adapter = factory.get("webhook")

    # 必须实现 ChannelAdapter（@runtime_checkable 支持 isinstance）
    assert isinstance(adapter, ChannelAdapter)
    # 类型判定
    assert isinstance(adapter, WebhookAdapter)
    assert adapter.channel_type == "webhook"
    # 错误 channel_type 必须抛 ValueError
    with pytest.raises(ValueError):
        factory.get("wechat")
