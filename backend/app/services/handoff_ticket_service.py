"""HandoffTicket 服务 - 人工介入工单的创建/排队/认领/回复结单

调用方：
- EscalationService.handoff() 末尾 persist_handoff()（best-effort，参照 audit_service 模式）
- app/api/admin_handoff.py 坐席工作台四端点

设计：
- 幂等合并：同 session 已有 pending/taken 工单时不新建，更新 summary/priority（防用户连点"转人工"刷屏队列）
- resolve 注入人工会话：Message(role=assistant, contexts={"human_agent_ticket": ...})——
  人工回复进同一条对话时间线，前端按 contexts 标"人工客服"徽标
- 独立 DB session（persist 侧）：异常只 warning，绝不阻断聊天主链路
"""
import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.handoff_ticket import HandoffStatus, HandoffTicket
from app.models.message import Message

logger = logging.getLogger(__name__)

# 优先级排序权重（列表展示用：P0 最前）
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def _new_ticket_no() -> str:
    return f"HFT-{uuid.uuid4().hex[:12].upper()}"


# =============================================================
# 创建（best-effort，EscalationService 调用侧无 db 依赖）
# =============================================================

def persist_handoff(
    user_id: int,
    session_id: str,
    reason: str,
    payload_dict: dict,
    category: Optional[str] = None,
    priority: Optional[str] = "P2",
    matched_keyword: Optional[str] = None,
    db: Optional[Session] = None,
) -> Optional[int]:
    """持久化一张转人工工单；同 session 未结单则合并更新。

    Args:
        db: 显式 session（测试/API 内复用）；None 时自开独立 session（audit 模式）
    Returns:
        ticket_id；失败返回 None（不抛，主链路无感）
    """
    own_session = db is None
    _db = db
    try:
        if own_session:
            from app.clients.mysql_client import get_session_local
            _db = get_session_local()()

        existing = _db.execute(
            select(HandoffTicket)
            .where(HandoffTicket.session_id == session_id)
            .where(HandoffTicket.status.in_([HandoffStatus.PENDING.value, HandoffStatus.TAKEN.value]))
            .where(HandoffTicket.deleted == 0)
            .order_by(HandoffTicket.id.desc())
            .limit(1)
        ).scalar_one_or_none() if session_id else None

        if existing is not None:
            # 合并：刷新上下文与优先级（升级词可能把 P2 抬到 P0）
            existing.summary = payload_dict
            if priority == "P0" or (existing.priority or "P2") == "P2":
                existing.priority = priority or existing.priority
            _db.commit()
            return existing.id

        ticket = HandoffTicket(
            ticket_no=_new_ticket_no(),
            session_id=session_id or "",
            user_id=user_id,
            reason=reason,
            category=category,
            priority=priority or "P2",
            matched_keyword=matched_keyword,
            status=HandoffStatus.PENDING.value,
            summary=payload_dict,
        )
        _db.add(ticket)
        _db.commit()
        _db.refresh(ticket)
        logger.info(
            f"handoff ticket created: no={ticket.ticket_no} user={user_id} "
            f"reason={reason} priority={ticket.priority}",
            extra={"user_id": user_id},
        )
        return ticket.id
    except Exception as e:  # best-effort：绝不阻断聊天
        logger.warning(f"persist_handoff failed (non-blocking): {e}")
        try:
            if _db is not None:
                _db.rollback()
        except Exception:
            pass
        return None
    finally:
        if own_session and _db is not None:
            try:
                _db.close()
            except Exception:
                pass


# =============================================================
# 坐席工作台读写（API 层持 db）
# =============================================================

def list_tickets(db: Session, status: Optional[str] = None, page: int = 1, page_size: int = 20) -> dict:
    """按状态排队列（P0 优先），返回 {total, page, items[dict]}"""
    stmt = select(HandoffTicket).where(HandoffTicket.deleted == 0)
    if status:
        stmt = stmt.where(HandoffTicket.status == status)
    rows = db.execute(stmt.order_by(HandoffTicket.id.desc()).limit(500)).scalars().all()
    rows = sorted(rows, key=lambda t: (_PRIORITY_ORDER.get(t.priority, 9), -t.id))
    total = len(rows)
    start = (max(1, page) - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_ticket_brief(t) for t in rows[start:start + page_size]],
    }


def get_ticket_detail(db: Session, ticket_id: int) -> Optional[dict]:
    """工单详情 + 会话最近 20 条消息（坐席看上下文用）"""
    t = db.get(HandoffTicket, ticket_id)
    if t is None or t.deleted:
        return None
    msgs = []
    if t.session_id:
        msgs = db.execute(
            select(Message)
            .where(Message.session_id == t.session_id)
            .where(Message.deleted == 0)
            .order_by(Message.id.desc())
            .limit(20)
        ).scalars().all()
        msgs = list(reversed(msgs))
    detail = _ticket_brief(t)
    detail["summary"] = t.summary
    detail["reply"] = t.reply
    detail["messages"] = [
        {
            "id": m.id, "role": m.role, "content": m.content,
            "create_time": m.create_time.isoformat() if m.create_time else None,
            "is_human_agent": bool(m.contexts and m.contexts.get("human_agent_ticket")),
        }
        for m in msgs
    ]
    return detail


def take_ticket(db: Session, ticket_id: int, admin_username: str) -> dict:
    """认领工单（pending→taken；已被他人认领返回 409 语义）"""
    t = db.get(HandoffTicket, ticket_id)
    if t is None or t.deleted:
        return {"ok": False, "code": 404, "error": "工单不存在"}
    if t.status != HandoffStatus.PENDING.value:
        return {"ok": False, "code": 409, "error": f"工单状态为 {t.status}，已被 {t.assignee or '—'} 处理"}
    t.status = HandoffStatus.TAKEN.value
    t.assignee = admin_username
    db.commit()
    return {"ok": True, "ticket": _ticket_brief(t)}


def resolve_ticket(db: Session, ticket_id: int, admin_username: str, reply: str) -> dict:
    """结单：人工回复注入会话（assistant + human_agent 标记）→ resolved

    注入后用户侧下次拉取会话历史即可看到人工回复（MySQL 冷路径为准）。
    """
    t = db.get(HandoffTicket, ticket_id)
    if t is None or t.deleted:
        return {"ok": False, "code": 404, "error": "工单不存在"}
    if t.status == HandoffStatus.RESOLVED.value:
        return {"ok": False, "code": 409, "error": "工单已结单"}
    if not (reply or "").strip():
        return {"ok": False, "code": 400, "error": "回复内容不能为空"}

    if t.session_id:
        msg = Message(
            session_id=t.session_id,
            user_id=t.user_id,
            role="assistant",
            content=reply.strip()[:2000],
            contexts={"human_agent_ticket": t.ticket_no, "agent_name": admin_username},
        )
        db.add(msg)
    t.status = HandoffStatus.RESOLVED.value
    t.reply = reply.strip()[:2000]
    if not t.assignee:
        t.assignee = admin_username
    db.commit()
    # V13（1.4）：人工回复只写了 MySQL，而用户拉历史 Redis 热路径优先
    # （session_service.load_history_with_fallback）——热会话未过期时用户
    # 永远看不到坐席回复（M15"回复注入会话"宣称的真实断链）。失效热缓存，
    # 下一次加载走 MySQL 冷回填。失败仅告警（TTL 600s 是最终兜底）。
    if t.session_id:
        try:
            from app.services import redis_store
            redis_store.clear_history(t.session_id)
        except Exception as e:
            logger.warning(
                f"resolve: redis history invalidate failed（热会话暂不可见人工回复）: {e}"
            )
    logger.info(f"handoff ticket resolved: no={t.ticket_no} by={admin_username}")
    return {"ok": True, "ticket": _ticket_brief(t)}


def close_ticket(db: Session, ticket_id: int, admin_username: str) -> dict:
    """关闭（不回复直接归档，如误触发/用户已离开）"""
    t = db.get(HandoffTicket, ticket_id)
    if t is None or t.deleted:
        return {"ok": False, "code": 404, "error": "工单不存在"}
    t.status = HandoffStatus.CLOSED.value
    if not t.assignee:
        t.assignee = admin_username
    db.commit()
    return {"ok": True, "ticket": _ticket_brief(t)}


def _ticket_brief(t: HandoffTicket) -> dict:
    return {
        "id": t.id,
        "ticket_no": t.ticket_no,
        "session_id": t.session_id,
        "user_id": t.user_id,
        "reason": t.reason,
        "category": t.category,
        "priority": t.priority,
        "matched_keyword": t.matched_keyword,
        "status": t.status,
        "assignee": t.assignee,
        "create_time": t.create_time.isoformat() if t.create_time else None,
        "update_time": t.update_time.isoformat() if t.update_time else None,
    }
