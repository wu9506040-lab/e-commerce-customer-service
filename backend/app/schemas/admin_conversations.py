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


# =============================================================
# P4-3：单对话 export（admin 审计 / SRE 排障 / CI 回归 fixture）
# =============================================================
class ConversationExportItem(BaseModel):
    """export 形式单条消息（与 AdminMessageItem 字段一致）

    字段说明：
    - role / content / create_time：基础消息字段
    - contexts / scores：RAG 召回原文与分数（policy 类消息必有，order/refund 类为空）
    - token_count / latency_ms：assistant 消息的成本与性能快照

    **不导出 intent / tool_calls**：Message ORM 当前未持久化这两个字段
    （intent 是运行时分类结果，tool_calls 是 FC 响应字段），export 阶段无法从
    messages 表读到。replay 端点会重新分类意图 + 重新调用工具（沙箱化），
    避免对历史中间态的依赖（CLAUDE.md §9.5.1 防幻觉 · 单一事实源原则）。
    """
    id: int = Field(..., description="消息 ID")
    index: int = Field(..., description="按 create_time 升序的序号（0-based）")
    role: str = Field(..., description="user / assistant")
    content: str = Field(..., description="消息原文")
    contexts: Optional[list] = Field(None, description="RAG 召回原文（list[dict]）")
    scores: Optional[list] = Field(None, description="RAG 相似度分数（list[float]）")
    token_count: Optional[int] = Field(None, description="LLM token 数")
    latency_ms: Optional[int] = Field(None, description="响应耗时")
    create_time: datetime = Field(..., description="创建时间")


class SystemSnapshot(BaseModel):
    """export 时的系统快照（用于 replay 时识别配置差异）

    用途：跨环境 replay 时如果 feature_flags 不同，结果可能不一致；
    记录当时的快照便于排查 "为什么同一对话在不同时间跑出不同结果"。
    """
    model: str = Field(..., description="LLM 模型名（如 qwen-plus）")
    prompt_version: str = Field(..., description="prompt 版本（如 agent.yaml@v3）")
    feature_flags: dict = Field(
        ..., description="关键灰度开关快照（ENABLE_* 全量）",
    )
    captured_at: datetime = Field(..., description="快照捕获时间")


class ConversationExport(BaseModel):
    """单对话 export 容器（P4-3 核心交付）

    用法：
    - admin 调 GET /admin/conversations/{sid}/export → 返回此结构
    - 前端触发 Content-Disposition: attachment 下载 .json 文件
    - 离线/CI 可用此 JSON 当 fixture 调 replay endpoint 复现
    """
    schema_version: str = Field("1.0", description="export schema 版本（升级时递增）")
    exported_at: datetime = Field(..., description="export 时间")
    exported_by: str = Field(..., description="admin username")
    conversation: dict = Field(
        ...,
        description="会话元信息（session_id / user_id / title / first_query / last_message_at 等）",
    )
    messages: List[ConversationExportItem] = Field(
        default_factory=list, description="消息列表（按 create_time 升序）",
    )
    system_snapshot: SystemSnapshot = Field(..., description="export 时的系统快照")


# =============================================================
# P4-3：replay（super_admin only · 用于 CI 回归 / 跨版本对比）
# =============================================================
class ConversationReplayRequest(BaseModel):
    """replay 请求体

    设计：只接受 query + history，不接受整个 export JSON。
    原因：export JSON 含 system_snapshot（不应被请求方覆写）；
    replay 时使用「当下」系统快照，便于 A/B 对比。
    """
    query: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    history: Optional[List[dict]] = Field(
        None, description="多轮历史（[{role, content}, ...]）；为空则单轮",
    )
    sku: Optional[str] = Field(None, description="上下文商品 SKU（模拟商品页跳转）")
    order_no: Optional[str] = Field(None, description="上下文订单号（模拟订单页跳转）")


class ConversationReplayResponse(BaseModel):
    """replay 响应（与 v12_rag_run_stream done 事件对齐）

    不持久化到 conversations/messages 表（CLAUDE.md §3.3 YAGNI + 副作用隔离）。
    """
    answer: str = Field(..., description="assistant 回复")
    intent: Optional[str] = Field(None, description="意图分类结果")
    entities: Optional[dict] = Field(None, description="实体抽取结果")
    latency_ms: int = Field(..., description="单轮 replay 耗时")
    replayed_at: datetime = Field(..., description="replay 时间")
    replayed_by: str = Field(..., description="super_admin username")
    system_snapshot: SystemSnapshot = Field(..., description="replay 时的系统快照（用于 A/B 对比）")
