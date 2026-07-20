"""
ChannelEvent / ChannelReply Schema（Sprint 14 · CLAUDE.md §9.9 ChannelAdapter 落地）

接入方（webhook / IM / 商城 / SaaS）通过 ChannelAdapter 把异构消息归一化为 ChannelEvent，
AI 客服处理后通过 ChannelReply 把回复投回接入方。

字段设计原则：
- channel_type/channel_user_id/channel_session_id 三元组定位一次会话
- metadata 留给接入方自定义字段（如 order_no / shop_id），不破坏通用契约
- message_type 默认 text；image/file/event 留扩展位（webhook 默认实现当前只解析 text）
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ChannelEvent(BaseModel):
    """标准化通道事件（ChannelAdapter.receive 出参）"""

    channel_type: str = Field(
        ..., description="通道类型（webhook / wechat / dingtalk / ...）",
    )
    channel_user_id: str = Field(..., description="接入方用户 ID")
    channel_session_id: str = Field(
        ..., description="接入方会话 ID（用于 send 时回传 context）",
    )
    message: str = Field(..., min_length=1, max_length=2000, description="消息文本")
    message_type: str = Field(
        "text", description="text | image | file | event",
    )
    metadata: dict = Field(
        default_factory=dict, description="接入方自定义字段（如 order_no / shop_id）",
    )
    timestamp: datetime = Field(..., description="事件时间")


class ChannelReply(BaseModel):
    """标准化 AI 回复（ChannelAdapter.send 入参）"""

    text: str = Field(..., description="回复文本")
    cards: Optional[List[dict]] = Field(
        None, description="结构化卡片（OrderCard / RefundCard 等，webhook 默认走 JSON）",
    )
    metadata: dict = Field(
        default_factory=dict, description="回调 metadata（回传接入方上下文）",
    )
