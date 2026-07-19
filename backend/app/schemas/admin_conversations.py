"""
admin_conversations 相关 Pydantic Schema

接口列表：
    GET  /admin/conversations                  全局会话列表（RBAC: admin）
    GET  /admin/conversations/{sid}/messages   单会话消息（RBAC: admin）

设计要点：
- 全部字段反序列化时脱敏（手机/邮箱 → ***）—— admin 视角不全量暴露用户 PII
- 强制时间窗：list 接口必须有 start_date + end_date，防止全表扫
- cursor 分页：复用 conversations.py 的 next_cursor 模式，保持一致
- 与 api/conversations.py 用户级接口分开（CLAUDE.md §9.2.2 Module Isolation）
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# =============================================================
# 共享：会话列表项（admin 视角）
# =============================================================
class AdminConversationListItem(BaseModel):
    """admin 全局会话列表项（含 user 信息 + 脱敏）"""
    session_id: str = Field(..., description="会话 ID")
    user_id: int = Field(..., description="归属用户 ID")
    username: str = Field(..., description="用户名（admin 可见）")
    user_display_name: Optional[str] = Field(None, description="用户显示名（admin 可见）")
    user_email_masked: Optional[str] = Field(
        None, description="用户邮箱（脱敏：a***@b.com 形式；仅 admin 可看脱敏后）",
    )
    title: Optional[str] = Field(None, description="会话标题")
    last_message: Optional[str] = Field(
        None, max_length=2000, description="最后一条消息内容",
    )
    message_count: int = Field(..., description="消息总数")
    handoff_count: int = Field(
        0, description="触发转人工次数（P0/P1/P2 累计；如能 join handoffs 表则统计，否则 0）",
    )
    last_message_at: Optional[datetime] = Field(None, description="最后消息时间")
    create_time: datetime = Field(..., description="会话创建时间")


class AdminConversationListResponse(BaseModel):
    """admin 全局会话列表响应"""
    conversations: List[AdminConversationListItem]
    total: int = Field(..., description="本页返回条数")
    next_cursor: Optional[str] = Field(
        None, description="下一页 cursor（last_message_at ISO8601 字符串）；无更多时 None",
    )
    has_more: bool = Field(..., description="是否还有更早的会话")
    filters_applied: dict = Field(
        ..., description="回显已应用的过滤参数（调试用）",
    )


# =============================================================
# 共享：消息项（admin 视角）
# =============================================================
class AdminMessageItem(BaseModel):
    """admin 视角单条消息（含 RAG 元信息 + 完整字段）"""
    id: int = Field(..., description="消息 ID")
    role: str = Field(..., description="user / assistant")
    content: str = Field(..., description="消息原文")
    contexts: Optional[list] = Field(
        None, description="RAG 召回原文（仅 assistant 消息有，反幻觉审计关键字段）",
    )
    scores: Optional[list] = Field(None, description="RAG 相似度分数")
    token_count: Optional[int] = Field(None, description="LLM token 数")
    latency_ms: Optional[int] = Field(None, description="响应耗时")
    create_time: datetime = Field(..., description="创建时间")


class AdminMessagesResponse(BaseModel):
    """admin 单会话消息响应"""
    session_id: str
    user_id: int = Field(..., description="会话归属用户 ID")
    messages: List[AdminMessageItem] = Field(default_factory=list)
    has_more: bool
    next_cursor: Optional[int] = Field(
        None, description="下一页 cursor（上一页最后一条 id）",
    )
    limit: int


# =============================================================
# 错误响应（统一格式）
# =============================================================
class AdminConvError(BaseModel):
    """admin 接口统一错误响应"""
    error: str = Field(..., description="错误码")
    message: str = Field(..., description="人类可读描述")
