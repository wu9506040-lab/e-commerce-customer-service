"""
Channels API（Sprint 14）

POST /api/channels/webhook/receive   接入方推送消息（签名校验 + 归一化）
POST /api/channels/webhook/send      AI 客服向接入方回送回复

错误码：InvalidSignatureError→401 / UnsupportedMessageTypeError→400 /
       RateLimitError→429 / SendMessageError→502
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.channels.protocols import (
    ChannelAdapter,
    ChannelAdapterFactory,
    InvalidSignatureError,
    RateLimitError,
    SendMessageError,
    UnsupportedMessageTypeError,
)
from app.channels.webhook_impl import WebhookAdapterFactory
from app.schemas.channel_event import ChannelEvent, ChannelReply

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/channels", tags=["channels"])


# MVP: 单实例（多租户 V3+ YAGNI）
_factory_instance: ChannelAdapterFactory | None = None


def get_factory() -> ChannelAdapterFactory:
    """FastAPI Depends：提供工厂单例（MVP secret/base_url 留空，由集成方运行时配置）"""
    global _factory_instance
    if _factory_instance is None:
        _factory_instance = WebhookAdapterFactory(secret="", base_url="")
    return _factory_instance


def get_adapter(
    factory: Annotated[ChannelAdapterFactory, Depends(get_factory)],
) -> ChannelAdapter:
    return factory.get("webhook")


@router.post("/webhook/receive", response_model=ChannelEvent, summary="Webhook 接收端点")
async def webhook_receive(
    request: Request,
    x_signature: Annotated[str | None, Header(alias="X-Signature")] = None,
    adapter: ChannelAdapter = Depends(get_adapter),
) -> ChannelEvent:
    """接收 webhook 消息并归一化"""
    body = await request.body()
    if hasattr(adapter, "_verify_signature"):
        adapter._verify_signature(body, x_signature)  # type: ignore[attr-defined]

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")

    try:
        return await adapter.receive(payload=payload, headers=dict(request.headers))
    except InvalidSignatureError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except UnsupportedMessageTypeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhook/send", summary="Webhook 发送端点（调试 / 集成验证用）")
async def webhook_send(
    event: ChannelEvent,
    reply: ChannelReply,
    adapter: ChannelAdapter = Depends(get_adapter),
) -> dict:
    """向接入方发送回复"""
    try:
        result = await adapter.send(event=event, reply=reply)
    except RateLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except SendMessageError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "result": result}
