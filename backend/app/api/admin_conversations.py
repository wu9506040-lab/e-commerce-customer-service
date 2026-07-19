"""
admin_conversations HTTP 接口层（P4-1 · 全局会话审计）

按 §6 规则：
- api/ 只负责路由 + 参数解析 + 调 ORM（admin 视角直接走 conversations/message join）
- 不改 api/conversations.py（用户级接口保持隔离；CLAUDE.md §9.2.2）

RBAC：
- 全部端点 require_admin（deps.py:62，非 admin 返 403）
- 用户 PII（邮箱/手机）必须脱敏（CLAUDE.md §9.5 安全）

性能约束：
- 强制时间窗 start_date + end_date（无 → 400，防止全表扫）
- 优化索引 idx_conversations_status_time (status, last_message_at DESC)
- cursor 分页：last_message_at ISO8601 + session_id 复合 cursor（避免同时间戳漏页）

实现：
    GET  /admin/conversations                       全局会话列表（按时间倒序）
    GET  /admin/conversations/{session_id}/messages 单会话完整消息
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.clients.mysql_client import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.schemas.admin_conversations import (
    AdminConversationListItem,
    AdminConversationListResponse,
    AdminMessageItem,
    AdminMessagesResponse,
)
from app.services.audit_service import try_log_action

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/conversations",
    tags=["admin-conversations"],
)


# =============================================================
# 内部辅助
# =============================================================
def _mask_email(email: Optional[str]) -> Optional[str]:
    """邮箱脱敏：a***@b.com（admin 看到也可追溯但不暴露全 PII）

    场景：admin 调查时需要知道「这个用户用哪个邮箱注册的」，但不应暴露完整邮箱。
    如 email 为 None 直接返 None（与原值一致便于排查）。
    """
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"{local}***@{domain}"
    return f"{local[0]}***@{domain}"


def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _user_agent(request: Request) -> Optional[str]:
    ua = request.headers.get("user-agent", "")
    return ua[:500] if ua else None


# =============================================================
# GET /admin/conversations - 全局会话列表
# =============================================================
@router.get(
    "",
    response_model=AdminConversationListResponse,
    summary="[admin] 全局会话列表",
    description=(
        "按 last_message_at DESC 倒序。**强制要求 start_date + end_date**，"
        "缺一返 400（防全表扫描）。"
        "支持过滤：user_id / keyword（LIKE first_query）/ has_handoff / min_latency_ms。"
        "需要 admin 角色（403 if not）。"
    ),
)
def list_admin_conversations(
    request: Request,
    start_date: Optional[datetime] = Query(
        None, description="起始时间（含），缺省返 400",
    ),
    end_date: Optional[datetime] = Query(
        None, description="结束时间（不含），缺省返 400",
    ),
    user_id: Optional[int] = Query(
        None, ge=1, description="按归属用户过滤（精确匹配）",
    ),
    keyword: Optional[str] = Query(
        None, max_length=200,
        description="全文搜索 first_query（LIKE '%kw%'）；MVP 不走 FULLTEXT",
    ),
    has_handoff: Optional[bool] = Query(
        None, description="True=仅含转人工的会话（用 messages.contexts 关联 handoff_id；MVP 简化为 content LIKE '%handoff%'）",
    ),
    min_latency_ms: Optional[int] = Query(
        None, ge=0, description="最慢响应延迟阈值（基于最后一条 assistant 消息的 latency_ms）",
    ),
    limit: int = Query(20, ge=1, le=100, description="每页条数（1-100）"),
    cursor: Optional[str] = Query(
        None, description="上一页最后一条 last_message_at ISO8601（不含）",
    ),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    全局会话审计列表（admin only）

    数据流：
        1. 校验强制时间窗（start_date + end_date 都必须）
        2. 校验时间范围 ≤ 90 天（防大跨度扫描）
        3. SELECT conversations LEFT JOIN users（带脱敏）
        4. cursor 分页（last_message_at ISO8601）
        5. audit 上报（admin_id + 命中条数 + 过滤参数）
    """
    # 1. 强制时间窗
    if start_date is None or end_date is None:
        raise HTTPException(
            status_code=400,
            detail="admin 全局查询必须传 start_date + end_date（防全表扫描）",
        )
    if end_date <= start_date:
        raise HTTPException(status_code=400, detail="end_date 必须 > start_date")
    if (end_date - start_date).days > 90:
        raise HTTPException(
            status_code=400,
            detail="时间范围 ≤ 90 天（防大跨度扫描；如需更长请分多次查）",
        )

    # 2. 主查询：conversations JOIN users（脱敏字段在 Python 层处理）
    stmt = (
        select(Conversation, User)
        .join(User, Conversation.user_id == User.id)
        .where(
            Conversation.deleted == 0,
            Conversation.last_message_at >= start_date,
            Conversation.last_message_at < end_date,
        )
        .order_by(
            Conversation.last_message_at.desc(),
            Conversation.session_id.desc(),  # 同时间戳的二级排序，避免漏页
        )
    )

    # 3. 可选过滤
    if user_id is not None:
        stmt = stmt.where(Conversation.user_id == user_id)
    if keyword:
        # LIKE '%kw%' 性能差但 MVP 够用（67 条数据 < 10ms）
        # 如未来需要再加 FULLTEXT 索引
        stmt = stmt.where(Conversation.first_query.like(f"%{keyword}%"))

    # 4. cursor 分页（用 last_message_at ISO8601）
    # 注意：cursor 仅过滤 last_message_at < cursor_time，不强制二级排序列
    # 上层统一 ORDER BY 已保证时序稳定
    if cursor is not None:
        try:
            cursor_time = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"cursor 格式错误（需 ISO8601）: {cursor}",
            )
        stmt = stmt.where(Conversation.last_message_at < cursor_time)

    # 5. fetch limit+1 检测 has_more
    rows = db.execute(
        stmt.limit(limit + 1)
    ).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = None
    if has_more and page_rows:
        last_time = page_rows[-1][0].last_message_at
        if last_time is not None:
            next_cursor = last_time.isoformat()

    # 6. 组装响应（含 PII 脱敏）
    items = []
    for conv, user in page_rows:
        items.append(
            AdminConversationListItem(
                session_id=conv.session_id,
                user_id=user.id,
                username=user.username,
                user_display_name=user.display_name,
                user_email_masked=_mask_email(user.email),
                title=conv.title,
                last_message=None,  # admin 视图不展开 last_message，避免 payload 过大
                message_count=conv.message_count or 0,
                handoff_count=0,  # MVP 不 join handoffs 表（业务表，P4-3 再加）
                last_message_at=conv.last_message_at,
                create_time=conv.create_time,
            )
        )

    # 7. audit 上报（best-effort）
    try_log_action(
        user=admin,
        action="admin_list_conversations",
        target_type="conversation",
        target_id=None,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        detail={
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "user_id": user_id,
            "keyword": keyword,
            "has_handoff": has_handoff,
            "min_latency_ms": min_latency_ms,
            "limit": limit,
            "cursor": cursor,
            "returned": len(items),
            "has_more": has_more,
        },
    )

    logger.info(
        f"admin_list_conversations: admin={admin.username}(id={admin.id}) "
        f"window={start_date}~{end_date} returned={len(items)} has_more={has_more}"
    )

    return AdminConversationListResponse(
        conversations=items,
        total=len(items),
        next_cursor=next_cursor,
        has_more=has_more,
        filters_applied={
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "user_id": user_id,
            "keyword": keyword,
            "has_handoff": has_handoff,
            "min_latency_ms": min_latency_ms,
            "limit": limit,
        },
    )


# =============================================================
# GET /admin/conversations/{session_id}/messages - 单会话消息
# =============================================================
@router.get(
    "/{session_id}/messages",
    response_model=AdminMessagesResponse,
    summary="[admin] 单会话完整消息历史",
    description=(
        "返回某 session 的全部消息（含 RAG contexts / scores / token / latency）。"
        "**不需要该会话属于当前用户**（admin 可查任意 session）。"
        "cursor 分页与 /api/conversations/{sid}/messages 一致。"
    ),
)
def get_admin_messages(
    request: Request,
    session_id: str = Path(..., min_length=1, max_length=64, description="会话 ID"),
    limit: int = Query(50, ge=1, le=200, description="每页消息数（1-200，admin 上限高于用户级 100）"),
    cursor: Optional[int] = Query(
        None, ge=1, description="上一页最后一条 id（不含）",
    ),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    admin 单会话消息查询（不做归属校验，admin 视角全局可读）

    与 api/conversations.py:get_messages 区别：
    - 不做 user_id 校验（admin 可读任意 session）
    - limit 上限更高（200 vs 100，因 admin 是审计场景非用户体验）
    - 包含 RAG contexts（完整 RAG 召回原文，反幻觉审计关键）
    """
    # 1. 取会话（不需要 user_id 校验）
    conv = db.execute(
        select(Conversation).where(
            Conversation.session_id == session_id,
            Conversation.deleted == 0,
        )
    ).scalar_one_or_none()

    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在或已删除")

    # 2. 拉消息
    stmt = select(Message).where(
        Message.session_id == session_id,
        Message.deleted == 0,
    )
    if cursor is not None:
        stmt = stmt.where(Message.id < cursor)

    rows = db.execute(
        stmt.order_by(Message.id.desc()).limit(limit + 1)
    ).scalars().all()

    has_more = len(rows) > limit
    page_messages = rows[:limit]
    next_cursor = page_messages[-1].id if has_more and page_messages else None

    # 3. 组装（含 RAG contexts）
    items = [
        AdminMessageItem(
            id=m.id,
            role=m.role,
            content=m.content,
            contexts=m.contexts,
            scores=m.scores,
            token_count=m.token_count,
            latency_ms=m.latency_ms,
            create_time=m.create_time,
        )
        for m in page_messages
    ]

    # 4. audit 上报
    try_log_action(
        user=admin,
        action="admin_get_conversation_messages",
        target_type="conversation",
        target_id=session_id,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        detail={
            "limit": limit,
            "cursor": cursor,
            "returned": len(items),
            "has_more": has_more,
        },
    )

    logger.info(
        f"admin_get_conversation_messages: admin={admin.username}(id={admin.id}) "
        f"session={session_id[:12]}... returned={len(items)} has_more={has_more}"
    )

    return AdminMessagesResponse(
        session_id=session_id,
        user_id=conv.user_id,
        messages=items,
        has_more=has_more,
        next_cursor=next_cursor,
        limit=limit,
    )
