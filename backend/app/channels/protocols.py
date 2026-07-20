"""
ChannelAdapter Protocol（CLAUDE.md §9.9 落地 · Sprint 14）

任意 IM / 商城 / SaaS 系统的接入抽象。
默认实现：Webhook（最通用）。其他实现：微信公众号 / 钉钉 / 飞书（V3+ YAGNI）。

三件套：
- ChannelAdapter：receive / send / get_user_info
- ChannelAdapterFactory：FastAPI Depends 注入入口（按 channel_type 分发）
- 5 个异常类：spec §1.2 完整覆盖
"""
from typing import Protocol, runtime_checkable

from app.schemas.channel_event import ChannelEvent, ChannelReply


@runtime_checkable
class ChannelAdapter(Protocol):
    """通道适配器协议 — 接入方实现该接口即可让 AI 客服对接自家系统"""

    channel_type: str  # "webhook" | "wechat" | "dingtalk" | ...

    async def receive(self, payload: dict, headers: dict) -> ChannelEvent:
        """
        接收外部系统的消息（webhook 调用 / 长连接回调）
        入参：原始 payload + headers（接入方自定义鉴权）
        出参：标准化 ChannelEvent（channel_user_id / message / metadata）
        异常：InvalidSignatureError / UnsupportedMessageTypeError
        """
        ...

    async def send(self, event: ChannelEvent, reply: ChannelReply) -> dict:
        """
        向外部系统发送 AI 回复
        入参：原始事件（用于回传 context）+ AI 回复
        出参：发送结果（接入方 API 返回值）
        异常：SendMessageError / RateLimitError
        """
        ...

    async def get_user_info(self, user_id: str) -> dict:
        """
        获取接入方用户信息（昵称 / 头像 / 等级 / 历史订单）
        接入方决定字段；AI 客服仅取 needed 字段
        """
        ...


class ChannelAdapterFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""

    def get(self, channel_type: str) -> ChannelAdapter: ...


# === 异常类（spec §1.2 全集） ===
class ChannelError(Exception):
    """所有 Channel 异常的基类 — 便于上层 except ChannelError 统一兜底"""


class InvalidSignatureError(ChannelError):
    """webhook / IM 签名校验失败（如 HMAC-SHA256 mismatch）"""


class UnsupportedMessageTypeError(ChannelError):
    """接入方发送的消息类型不在 ChannelAdapter 支持范围（如 image / file / event）"""


class SendMessageError(ChannelError):
    """send() 阶段除 RateLimit 外的失败（4xx / 5xx / 网络错误）"""


class RateLimitError(ChannelError):
    """send() 阶段被接入方限流（HTTP 429）"""
