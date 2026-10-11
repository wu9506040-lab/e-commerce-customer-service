"""badcase 自动池服务 - V14（2.4 飞轮机制化）

把简历里"badcase 采集→人工审核→回归复验"从人肉环节做成机制：
三类信号（👎 / LLM 低置信 / guard 拦截）入池，admin 审核标注
expected_source（稳定键），`scripts/merge_badcase_to_eval.py` 半自动
合入评测集并回写 merged。

设计原则（承接 2.3 教训"降级必须显形"）：
- 入池失败绝不吞——调用方 try/except 后 error 计数 + 日志，但主链路
  （评价写入 / chat 应答）永不因入池失败被阻断；
- 同问句同信号唯一（uq_badcase_signal_query），复发 bump hit_count 不重复建行；
- 状态机只允许合法迁移，非法迁移抛 ValueError 由 API 层转 400。
"""
import datetime as dt
import hashlib
import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.badcase import (
    STATUS_DISCARDED, STATUS_MERGED, STATUS_NEW, STATUS_REVIEWED,
    BadcaseCandidate,
)
from app.models.message import Message
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

# 默认低置信阈值：LLM 判的且 conf 低于它，路由质量存疑，值得人看一眼
LOW_CONF_THRESHOLD = 0.6
_ANSWER_EXCERPT = 200
_CONTEXT_EXCERPT = 160
_CONTEXT_TOP_N = 3
_QUERY_MAX_LEN = 2000

# 合法迁移表（merged 只由 merge 脚本回写，不开放人工设置）
# reviewed -> reviewed 允许：修正 expected_source/note 的再审核
_TRANSITIONS = {
    STATUS_REVIEWED: {STATUS_NEW, STATUS_DISCARDED, STATUS_REVIEWED},
    STATUS_DISCARDED: {STATUS_NEW, STATUS_REVIEWED},
    STATUS_NEW: {STATUS_REVIEWED, STATUS_DISCARDED},
}


def normalize_query(query: str) -> str:
    """归一化用于去重哈希：去首尾空白、连续空白折叠为单空格。"""
    return re.sub(r"\s+", " ", (query or "").strip())


def query_hash(query: str) -> str:
    return hashlib.md5(normalize_query(query).encode("utf-8")).hexdigest()


def _brief_contexts(contexts: Optional[List[Any]],
                    scores: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """检索现场摘要。contexts 列历史上存在两种形状（str 原文 / dict 带 payload），
    两种都要能吃下——这里兼容取值，不做二选一假设。"""
    brief: List[Dict[str, Any]] = []
    for i, c in enumerate((contexts or [])[:_CONTEXT_TOP_N]):
        source: Optional[str] = None
        text: str = ""
        if isinstance(c, dict):
            payload = c.get("payload") or {}
            source = c.get("source") or payload.get("source")
            text = c.get("text") or c.get("content") or ""
        elif isinstance(c, str):
            text = c
        score = None
        if scores and i < len(scores) and isinstance(scores[i], (int, float)):
            score = round(float(scores[i]), 4)
        brief.append({
            "source": source,
            "score": score,
            "excerpt": text[:_CONTEXT_EXCERPT],
        })
    return brief


def _user_query_before(db: Session, *, session_id: str,
                       assistant_id: int) -> Optional[Message]:
    """取该 assistant 消息之前最近的一条 user 消息（同会话，按 id 时序）。"""
    return db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.role == "user",
            Message.deleted == 0,
            Message.id < assistant_id,
        )
        .order_by(Message.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _find_active(db: Session, *, qhash: str, signal: str) -> Optional[BadcaseCandidate]:
    return db.execute(
        select(BadcaseCandidate).where(
            BadcaseCandidate.query_hash == qhash,
            BadcaseCandidate.signal == signal,
            BadcaseCandidate.deleted == 0,
        )
    ).scalar_one_or_none()


def enqueue(db: Session, *, signal: str, query: str, session_id: str = "",
            message_id: int = 0, user_id: int = 0,
            snapshot: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """入池/复发 bump。空问句返回 None（没证据价值）；写失败抛异常由调用方显形。"""
    q = normalize_query(query)[:_QUERY_MAX_LEN]
    if not q:
        return None
    qhash = query_hash(q)
    existing = _find_active(db, qhash=qhash, signal=signal)
    now = dt.datetime.now()  # 显式本地时间：SQLite CURRENT_TIMESTAMP 是 UTC，防 8h 偏差
    if existing is not None:
        existing.hit_count += 1
        existing.update_time = now
        db.commit()
        metrics.badcase_enqueue_total += 1
        metrics.badcase_by_signal[signal] = metrics.badcase_by_signal.get(signal, 0) + 1
        return {"action": "bumped", "id": existing.id, "status": existing.status,
                "hit_count": existing.hit_count}
    row = BadcaseCandidate(
        signal=signal, session_id=session_id or "", message_id=message_id,
        user_id=user_id, query=q, query_hash=qhash, snapshot=snapshot or {},
        status=STATUS_NEW, hit_count=1, create_time=now, update_time=now,
    )
    db.add(row)
    db.commit()
    metrics.badcase_enqueue_total += 1
    metrics.badcase_by_signal[signal] = metrics.badcase_by_signal.get(signal, 0) + 1
    return {"action": "created", "id": row.id, "status": STATUS_NEW, "hit_count": 1}


def enqueue_thumbs_down(db: Session, *, session_id: str, message_id: int,
                        user_id: int = 0) -> Optional[Dict[str, Any]]:
    """👎 入池：反查当轮 assistant 消息（问句/检索现场/意图快照），join 2.3 地基。

    message_id=0（旧版会话粒度评价）时兜底取该会话最后一条 assistant 消息。
    找不到任何现场 → 记 error 计数返回 None，不静默。"""
    assistant: Optional[Message] = None
    fallback_used = False
    if message_id:
        assistant = db.get(Message, message_id)
        if assistant is not None and assistant.deleted:
            assistant = None
    if assistant is None:
        assistant = db.execute(
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.role == "assistant",
                Message.deleted == 0,
            )
            .order_by(Message.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        fallback_used = assistant is not None
    if assistant is None:
        metrics.badcase_enqueue_error += 1
        logger.warning(
            "badcase enqueue thumbs_down no-scene: session=%s message_id=%s",
            session_id, message_id,
        )
        return None
    user_msg = _user_query_before(db, session_id=session_id, assistant_id=assistant.id)
    if user_msg is None:
        metrics.badcase_enqueue_error += 1
        logger.warning(
            "badcase enqueue thumbs_down no-user-query: session=%s assistant=%s",
            session_id, assistant.id,
        )
        return None
    snapshot = {
        "intent": assistant.intent_snapshot,
        "answer_excerpt": (assistant.content or "")[:_ANSWER_EXCERPT],
        "top_contexts": _brief_contexts(assistant.contexts, assistant.scores),
        "message_id_fallback": fallback_used,
        "assistant_message_id": assistant.id,
    }
    return enqueue(db, signal="thumbs_down", query=user_msg.content,
                   session_id=session_id, message_id=assistant.id,
                   user_id=user_id, snapshot=snapshot)


def enqueue_guard_blocked(db: Session, *, query: str, session_id: str,
                          user_id: int, layer: str, reason: str) -> Optional[Dict[str, Any]]:
    """guard 拦截留证入池（operation_log 不记正文，误杀只能靠这里）。"""
    snapshot = {"guard_layer": layer, "guard_reason": reason}
    return enqueue(db, signal="guard_blocked", query=query,
                   session_id=session_id, user_id=user_id, snapshot=snapshot)


def enqueue_guard_blocked_safe(*, query: str, session_id: str, user_id: int,
                               layer: str, reason: str) -> None:
    """chat guard 分支的请求路径独立变体：自开 session、失败显形（计数+日志）
    但绝不外抛——拦截应答主链路不因留证失败而中断。"""
    from app.clients.mysql_client import get_session_local
    db = None
    try:
        db = get_session_local()()
        enqueue_guard_blocked(db, query=query, session_id=session_id,
                              user_id=user_id, layer=layer, reason=reason)
    except Exception:
        metrics.badcase_enqueue_error += 1
        logger.exception("badcase enqueue guard_blocked failed (blocked-reply unaffected)")
    finally:
        if db is not None:
            db.close()


def collect_low_confidence(db: Session, *, since: Optional[dt.datetime] = None,
                           threshold: float = LOW_CONF_THRESHOLD,
                           limit: int = 200) -> Dict[str, Any]:
    """扫描 assistant 消息的 intent_snapshot：method=llm 且 confidence<threshold
    → 入池。规则命中（method=rule）与高置信不收。"""
    stmt = (
        select(Message)
        .where(
            Message.role == "assistant",
            Message.deleted == 0,
            Message.intent_snapshot.is_not(None),
        )
        .order_by(Message.id.desc())
        .limit(max(1, min(int(limit), 1000)) * 5)  # 快照非低置信的占多数，放宽抓取
    )
    if since is not None:
        stmt = stmt.where(Message.create_time >= since)
    rows = db.execute(stmt).scalars().all()
    scanned = 0
    created = bumped = skipped = 0
    for msg in rows:
        snap = msg.intent_snapshot or {}
        if snap.get("method") != "llm":
            continue
        scanned += 1
        conf = snap.get("confidence")
        if not isinstance(conf, (int, float)) or conf >= threshold:
            continue
        user_msg = _user_query_before(db, session_id=msg.session_id, assistant_id=msg.id)
        if user_msg is None:
            skipped += 1
            continue
        result = enqueue(
            db, signal="low_confidence", query=user_msg.content,
            session_id=msg.session_id, message_id=msg.id, user_id=msg.user_id,
            snapshot={
                "intent": snap,
                "answer_excerpt": (msg.content or "")[:_ANSWER_EXCERPT],
                "top_contexts": _brief_contexts(msg.contexts, msg.scores),
                "assistant_message_id": msg.id,
            },
        )
        if result is None:
            skipped += 1
        elif result["action"] == "created":
            created += 1
        else:
            bumped += 1
    return {"scanned": scanned, "created": created, "bumped": bumped,
            "skipped_no_query": skipped, "threshold": threshold}


def list_candidates(db: Session, *, status: Optional[str] = None,
                    signal: Optional[str] = None, page: int = 1,
                    page_size: int = 20) -> Dict[str, Any]:
    conds = [BadcaseCandidate.deleted == 0]
    if status:
        conds.append(BadcaseCandidate.status == status)
    if signal:
        conds.append(BadcaseCandidate.signal == signal)
    total = db.execute(
        select(sa_func.count()).select_from(BadcaseCandidate).where(*conds)
    ).scalar() or 0
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    rows = db.execute(
        select(BadcaseCandidate).where(*conds)
        .order_by(BadcaseCandidate.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {
        "items": [_to_dict(r) for r in rows],
        "total": int(total), "page": page, "pageSize": page_size,
        "totalPages": (int(total) + page_size - 1) // page_size,
    }


def review(db: Session, *, badcase_id: int, action: str, admin_id: int,
           expected_source: Optional[str] = None,
           note: Optional[str] = None) -> Dict[str, Any]:
    """人工审核：new/discarded ↔ reviewed、new/reviewed → discarded。
    merged 行不回退（评测集条目删除走 eval 侧，池内保留合入历史）。"""
    if action not in (STATUS_REVIEWED, STATUS_DISCARDED):
        raise ValueError(f"action 只支持 {STATUS_REVIEWED}/{STATUS_DISCARDED}")
    row = db.get(BadcaseCandidate, badcase_id)
    if row is None or row.deleted:
        raise LookupError("badcase 不存在")
    if row.status == STATUS_MERGED:
        raise ValueError("已合入评测集的行不可再审核")
    if row.status not in _TRANSITIONS.get(action, set()):
        raise ValueError(f"非法状态迁移: {row.status} -> {action}")
    row.status = action
    row.reviewed_by = admin_id
    if expected_source is not None:
        row.expected_source = expected_source.strip()[:200] or None
    if note is not None:
        row.note = note[:500]
    row.update_time = dt.datetime.now()
    db.commit()
    return _to_dict(row)


def get_by_id(db: Session, badcase_id: int) -> Optional[Dict[str, Any]]:
    row = db.get(BadcaseCandidate, badcase_id)
    if row is None or row.deleted:
        return None
    return _to_dict(row)


def mark_merged(db: Session, *, badcase_id: int, note: str) -> None:
    """merge 脚本专用回写：reviewed → merged（绕过人工迁移表，幂等由调用方保证）。"""
    row = db.get(BadcaseCandidate, badcase_id)
    if row is None or row.deleted:
        raise LookupError(f"badcase {badcase_id} 不存在")
    if row.status != STATUS_REVIEWED:
        raise ValueError(f"只允许 {STATUS_REVIEWED} -> merged，当前 {row.status}")
    row.status = STATUS_MERGED
    row.merged_at = dt.datetime.now()
    row.note = note[:500]
    row.update_time = row.merged_at
    db.commit()


def _iso(v: Any) -> Optional[str]:
    return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, dt.datetime) else None


def _to_dict(row: BadcaseCandidate) -> Dict[str, Any]:
    return {
        "id": row.id, "signal": row.signal, "status": row.status,
        "session_id": row.session_id, "message_id": row.message_id,
        "user_id": row.user_id, "query": row.query,
        "expected_source": row.expected_source, "note": row.note,
        "reviewed_by": row.reviewed_by, "hit_count": row.hit_count,
        "snapshot": row.snapshot or {},
        "merged_at": _iso(row.merged_at),
        "create_time": _iso(row.create_time), "update_time": _iso(row.update_time),
    }
