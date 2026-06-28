"""
Conversation API - 会话历史读取层（V1 收口，§12）

按 §6 规则：api/ 只负责路由 + 参数解析 + 调 services
本模块：新增 3 个会话历史端点，不改 /chat、不改 RAG pipeline

实现：
    GET    /conversations                       当前用户所有会话列表
    GET    /conversations/{session_id}/messages 单会话完整消息历史
    DELETE /conversations/{session_id}          软删除会话（保留审计）

约束（按任务约束）：
- 复用现有 ORM（Conversation / Message）
- 复用 get_db / get_current_user / try_log_action（不复制 SQL session 样板）
- Redis 仅作为缓存加速（清理用），主数据源是 MySQL
"""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.clients.mysql_client import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.audit_service import try_log_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


# =============================================================
# Pydantic Response Schemas
# =============================================================
class ConversationListItem(BaseModel):
    """会话列表项"""
    session_id: str = Field(..., description="会话 ID")
    title: Optional[str] = Field(
        None, max_length=200,
        description="会话标题（M9 自动生成：首条 user 消息前 20 字符）",
    )
    last_message: Optional[str] = Field(
        None, max_length=2000,
        description="最后一条消息内容（assistant 回复，按 create_time 取最新）",
    )
    updated_at: Optional[datetime] = Field(None, description="最后消息时间")
    message_count: int = Field(
        ..., description="消息总数（user + assistant 各算 1 条）",
    )


class ConversationListResponse(BaseModel):
    """会话列表响应"""
    conversations: List[ConversationListItem]
    total: int = Field(..., description="返回的会话数")


class MessageItem(BaseModel):
    """单条消息"""
    role: str = Field(..., description="user / assistant")
    content: str = Field(..., description="消息内容")
    contexts: Optional[list] = Field(
        None, description="RAG 检索的 context 列表（仅 assistant）",
    )
    scores: Optional[list] = Field(
        None, description="对应相似度分数（仅 assistant）",
    )
    create_time: datetime = Field(..., description="消息创建时间")


class MessagesResponse(BaseModel):
    """消息列表响应（cursor 分页）"""
    session_id: str = Field(..., description="会话 ID")
    messages: List[MessageItem] = Field(
        default_factory=list, description="本页消息列表（按 id 倒序）",
    )
    has_more: bool = Field(
        ..., description="是否还有更早的消息（向上翻页）",
    )
    next_cursor: Optional[int] = Field(
        None, description="下一页 cursor：当前页最后一条消息的 id。无更多时为 null。",
    )
    limit: int = Field(..., description="本次请求的 limit（已应用）")


class DeleteResponse(BaseModel):
    """删除响应"""
    session_id: str
    message: str = "已删除"


class TitleUpdateRequest(BaseModel):
    """标题更新请求"""
    title: str = Field(
        ..., min_length=1, max_length=200,
        description="新标题（1-200 字符）",
    )


class TitleUpdateResponse(BaseModel):
    """标题更新响应"""
    session_id: str
    title: str
    message: str = "标题已更新"


# =============================================================
# 内部辅助（与 api/chat.py / api/admin.py 保持一致）
# =============================================================
def _client_ip(request: Request) -> Optional[str]:
    """取客户端 IP（优先 X-Forwarded-For，再 client.host）"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _user_agent(request: Request) -> Optional[str]:
    """取 UA，截断 500 字符（匹配 operation_logs 字段长度）"""
    ua = request.headers.get("user-agent", "")
    return ua[:500] if ua else None


# =============================================================
# GET /conversations - 当前用户所有会话列表
# =============================================================
@router.get(
    "",
    response_model=ConversationListResponse,
    summary="获取当前用户的所有会话",
    description=(
        "按 last_message_at 倒序返回。Redis 不作为主数据源，本接口直接读 MySQL。"
        "需要登录。"
    ),
)
def list_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    列出当前用户所有未删除会话

    数据流：
        1. SELECT conversations WHERE user_id=? AND deleted=0 ORDER BY last_message_at DESC
        2. 批量 SELECT messages WHERE session_id IN (...) ORDER BY (session_id, create_time DESC)
        3. Python 聚合每会话最后一条消息（取每组第一条 = 最新一条）
        4. 组装 ConversationListItem 返回
    """
    # 1. 查会话列表
    # 注：MySQL 不支持 NULLS LAST 语法，但 last_message_at 由 persist_to_mysql
    #     写入时必带 func.now()，实际不会 NULL；DESC 在 MySQL 中天然 NULL 末尾。
    rows = db.execute(
        select(Conversation)
        .where(
            Conversation.user_id == current_user.id,
            Conversation.deleted == 0,
        )
        .order_by(Conversation.last_message_at.desc())
    ).scalars().all()

    if not rows:
        return ConversationListResponse(conversations=[], total=0)

    # 2. 单 SQL 批量取每会话最后一条消息（§13 优化：GROUP BY + JOIN subquery）
    #    子查询：按 session_id GROUP BY，取 MAX(id)（即最后插入的 message = assistant 回复）
    #    JOIN 回 messages 表取 content
    #    单次 SQL 完成，避免 Python 循环聚合或 N+1
    session_ids = [r.session_id for r in rows]
    max_id_subq = (
        select(
            Message.session_id.label("sid"),
            func.max(Message.id).label("max_id"),
        )
        .where(Message.session_id.in_(session_ids), Message.deleted == 0)
        .group_by(Message.session_id)
        .subquery()
    )
    last_msg_rows = db.execute(
        select(Message.session_id, Message.content)
        .join(
            max_id_subq,
            (Message.session_id == max_id_subq.c.sid)
            & (Message.id == max_id_subq.c.max_id),
        )
    ).all()
    last_msg_map = {row.session_id: row.content for row in last_msg_rows}

    # 4. 组装响应
    items = [
        ConversationListItem(
            session_id=r.session_id,
            title=r.title,
            last_message=last_msg_map.get(r.session_id),
            updated_at=r.last_message_at,
            message_count=r.message_count or 0,
        )
        for r in rows
    ]

    logger.info(
        f"list_conversations: user={current_user.id}, "
        f"returned={len(items)} sessions"
    )

    return ConversationListResponse(conversations=items, total=len(items))


# =============================================================
# GET /conversations/{session_id}/messages - 单会话完整消息历史
# =============================================================
@router.get(
    "/{session_id}/messages",
    response_model=MessagesResponse,
    summary="获取会话消息（cursor 分页）",
    description=(
        "按 id DESC 倒序（新→旧）。"
        "limit 默认 20，上限 100。"
        "下一页传 next_cursor（上一页最后一条 id）。"
        "需要登录且该会话属于当前用户。"
    ),
)
def get_messages(
    session_id: str = Path(..., min_length=1, max_length=64, description="会话 ID"),
    limit: int = Query(20, ge=1, le=100, description="每页消息数（1-100）"),
    cursor: Optional[int] = Query(
        None, ge=1,
        description="上一页最后一条消息的 id（不含）。首次请求不传。",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    获取单会话消息历史（cursor 分页）

    分页逻辑：
        1. 首次请求：cursor=None，返回最新 limit 条（id DESC）
        2. 后续请求：cursor=上一页最后一条 id，返回 id < cursor 的 limit 条
        3. has_more 检测：fetch limit+1 条，多取 1 条判断边界
        4. next_cursor = 当前页最后一条 id（仅当 has_more=True 时返回）

    权限：
        - 必须登录（get_current_user 401 兜底）
        - 会话必须属于当前用户（否则 404，不暴露存在性）
    """
    # 1. 权限校验
    conv = db.execute(
        select(Conversation).where(
            Conversation.session_id == session_id,
            Conversation.user_id == current_user.id,
            Conversation.deleted == 0,
        )
    ).scalar_one_or_none()

    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    # 2. 单 SQL 查询（带可选 cursor）
    stmt = select(Message).where(
        Message.session_id == session_id,
        Message.deleted == 0,
    )
    if cursor is not None:
        stmt = stmt.where(Message.id < cursor)

    # 3. fetch limit+1 检测 has_more（多取 1 条）
    rows = db.execute(
        stmt.order_by(Message.id.desc()).limit(limit + 1)
    ).scalars().all()

    # 4. 计算分页元数据
    has_more = len(rows) > limit
    page_messages = rows[:limit]
    next_cursor = page_messages[-1].id if has_more and page_messages else None

    # 5. 组装响应
    items = [
        MessageItem(
            role=r.role,
            content=r.content,
            contexts=r.contexts,
            scores=r.scores,
            create_time=r.create_time,
        )
        for r in page_messages
    ]

    logger.info(
        f"get_messages: user={current_user.id}, session={session_id[:12]}..., "
        f"limit={limit}, cursor={cursor}, returned={len(items)}, has_more={has_more}"
    )

    return MessagesResponse(
        session_id=session_id,
        messages=items,
        has_more=has_more,
        next_cursor=next_cursor,
        limit=limit,
    )


# =============================================================
# DELETE /conversations/{session_id} - 软删除
# =============================================================
@router.delete(
    "/{session_id}",
    response_model=DeleteResponse,
    summary="软删除会话",
    description=(
        "标记 conversations.deleted=1 和 messages.deleted=1。"
        "会话不再出现在列表中，消息保留供审计。"
        "Redis 缓存同步清理（best-effort）。"
    ),
)
def delete_conversation(
    request: Request,
    session_id: str = Path(..., min_length=1, max_length=64, description="会话 ID"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    软删除会话（单事务内保证 consistency）

    操作顺序：
        1. 校验所有权（404 if 不属于当前用户）
        2. UPDATE conversations.deleted=1（已加载对象直接修改）
        3. UPDATE messages.deleted=1（同一 session）
        4. 单事务 commit（保证一致性，失败自动回滚）
        5. Redis 缓存清理（best-effort，独立失败不影响主流程）
        6. audit 上报（best-effort，复用 try_log_action）
    """
    # 1. 权限校验
    conv = db.execute(
        select(Conversation).where(
            Conversation.session_id == session_id,
            Conversation.user_id == current_user.id,
            Conversation.deleted == 0,
        )
    ).scalar_one_or_none()

    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    # 记录删除前的消息数（用于 audit detail）
    message_count_before = conv.message_count or 0

    # 2. 软删除 conversation（修改已加载对象，commit 时自动 UPDATE）
    conv.deleted = 1

    # 3. 软删除 messages（同一事务，保证 atomic）
    db.execute(
        update(Message)
        .where(Message.session_id == session_id, Message.deleted == 0)
        .values(deleted=1)
    )

    # 4. 提交事务
    db.commit()

    # 5. Redis 缓存清理（best-effort，不阻塞主流程）
    # 复用 session_service.clear_history() 已存在的 helper，不重写 session 逻辑
    try:
        from app.services.session_service import clear_history
        clear_history(session_id)
    except Exception as e:
        logger.warning(
            f"Redis 清理失败（不影响 MySQL 主流程）: "
            f"session={session_id[:12]}..., {e}"
        )

    # 6. audit 上报（best-effort，复用 try_log_action）
    ip = _client_ip(request)
    ua = _user_agent(request)
    try_log_action(
        user=current_user,
        action="delete_conversation",
        target_type="conversation",
        target_id=session_id,
        ip=ip,
        user_agent=ua,
        detail={"message_count": message_count_before},
    )

    logger.info(
        f"delete_conversation: user={current_user.id}, "
        f"session={session_id[:12]}..., messages_deleted~{message_count_before}"
    )

    return DeleteResponse(session_id=session_id)


# =============================================================
# PATCH /conversations/{session_id} - 更新标题
# =============================================================
@router.patch(
    "/{session_id}",
    response_model=TitleUpdateResponse,
    summary="更新会话标题",
    description=(
        "前端在首条消息发出后，自动用首条 user 消息的前 N 字符作标题。"
        "需要登录且该会话属于当前用户。"
    ),
)
def update_conversation_title(
    payload: TitleUpdateRequest,
    session_id: str = Path(..., min_length=1, max_length=64, description="会话 ID"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    更新会话标题

    用途：M9 前端自动生成标题（首条 user 消息前 20 字符）。
    仅修改 title 字段，不动 message_count / last_message_at。
    """
    # 1. 权限校验（同时确认存在性）
    conv = db.execute(
        select(Conversation).where(
            Conversation.session_id == session_id,
            Conversation.user_id == current_user.id,
            Conversation.deleted == 0,
        )
    ).scalar_one_or_none()

    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    # 2. 修剪 + 更新（前端可能传了首尾空白）
    new_title = payload.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if len(new_title) > 200:
        new_title = new_title[:200]

    conv.title = new_title
    db.commit()

    logger.info(
        f"update_conversation_title: user={current_user.id}, "
        f"session={session_id[:12]}..., title_len={len(new_title)}"
    )

    return TitleUpdateResponse(session_id=session_id, title=new_title)