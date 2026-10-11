"""V14（human-in-the-loop）confirm_ticket 服务

业务铁律（来自培训机构行业共识 + 本项目弹药 §12）：
- AI 出提案 → 写 confirm_tickets（proposed）→ 返 ticket_id + summary
- 人点确认（前端从 SSE / list 拉到卡片 → POST /confirm/{id}/confirm）
- 服务端校验通过（status=proposed、未过期、args_hash 一致、归属）→ 调执行器 → 状态推进

5 道防御（详见 README 业务铁律段）：
1. 参数冻结：args_hash 写入时算一次，confirm 时再算——不一致拒绝；防"AI 偷偷改金额"
2. TTL：默认 300 秒过期未操作变 expired——隔夜票据不能 confirm
3. 幂等：同 ticket 二次 confirm 返原 result_snapshot，不重复执行（防双花）
4. 状态机：proposed → confirmed（confirm 入口）/ rejected（reject 入口）；merged 只能 reviewed → executed
5. 全量 timestamp + result_snapshot：proposed/confirmed/executed/expired 四事件均可审计回放

执行器注册：TOOL_EXECUTORS[tool_name] = callable(args, user_id) -> dict
服务冷启动 lazy-import 真正执行器以避免循环依赖
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.confirm_ticket import (
    ConfirmTicket,
    STATUS_CONFIRMED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
)
from app.services.metrics import metrics as _metrics

logger = logging.getLogger(__name__)

DEFAULT_TTL_SEC = 300  # 5 分钟未确认自动过期
HASH_PREFIX = "ct:"  # 防与 badcase 等其他 md5 冲突

# 工具执行器注册表：tool_name -> callable(args: dict, user_id: int) -> dict
# 业务模块 init 时填充（lazy import 避免循环依赖）
TOOL_EXECUTORS: Dict[str, Callable[[Dict[str, Any], int], Dict[str, Any]]] = {}


def register_executor(tool_name: str,
                     executor: Callable[[Dict[str, Any], int], Dict[str, Any]]) -> None:
    """业务模块启动时注册高风险工具的执行器（与 ToolSpec.risk_level 配合）"""
    TOOL_EXECUTORS[tool_name] = executor


def canonical_json(payload: Any) -> str:
    """稳定 JSON：键排序、去空格——保证同 args 算出来同 hash（防 dict 顺序漂移）"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def args_hash(args: Dict[str, Any]) -> str:
    return HASH_PREFIX + hashlib.md5(canonical_json(args).encode("utf-8")).hexdigest()


# =============================================================
# 写入侧：propose
# =============================================================
def propose(
    db: Session,
    *,
    tool_name: str,
    args: Dict[str, Any],
    summary: str,
    user_id: int,
    session_id: str = "",
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> Dict[str, Any]:
    """AI 侧调用：把"想做什么"以结构化提案落库；人尚未确认。"""
    if tool_name not in TOOL_EXECUTORS:
        raise ValueError(f"unknown high-risk tool: {tool_name!r} (未注册执行器)")
    a_hash = args_hash(args)
    expires_at = dt.datetime.now() + dt.timedelta(seconds=ttl_sec)
    # 唯一索引去重：同会话同工具同参数已有 proposed/confirmed 行则 bump 那条而非新建
    existing = db.execute(
        select(ConfirmTicket).where(
            ConfirmTicket.session_id == session_id,
            ConfirmTicket.tool_name == tool_name,
            ConfirmTicket.args_hash == a_hash,
            ConfirmTicket.status.in_([STATUS_PROPOSED, STATUS_CONFIRMED]),
            ConfirmTicket.deleted == 0,
        )
    ).scalar_one_or_none()
    now = dt.datetime.now()
    if existing is not None:
        existing.expires_at = expires_at
        existing.update_time = now
        db.commit()
        return _to_dict(existing)
    row = ConfirmTicket(
        tool_name=tool_name,
        tool_args=args,
        args_hash=a_hash,
        summary=summary[:500],
        session_id=session_id or "",
        user_id=user_id,
        status=STATUS_PROPOSED,
        expires_at=expires_at,
        create_time=now,
        update_time=now,
    )
    db.add(row)
    db.commit()
    _metrics.record_confirm_ticket_propose(tool_name)
    logger.info(
        f"confirm_ticket.propose id={row.id} tool={tool_name} user={user_id} "
        f"summary={summary[:60]!r}"
    )
    return _to_dict(row)


# =============================================================
# 读取侧：list / get
# =============================================================
def list_for_user(db: Session, *, user_id: int, status: Optional[str] = None,
                  page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    conds = [ConfirmTicket.user_id == user_id, ConfirmTicket.deleted == 0]
    if status:
        conds.append(ConfirmTicket.status == status)
    else:
        # 默认只暴露"还需用户操作"的票：proposed + 未过期；executed/rejected/confirmed 已静默
        conds.append(ConfirmTicket.status == STATUS_PROPOSED)
    total = db.execute(
        select(ConfirmTicket.id).where(*conds)
    ).all()
    total_n = len(total)
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 50))
    rows = db.execute(
        select(ConfirmTicket).where(*conds)
        .order_by(ConfirmTicket.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {
        "items": [_to_dict(r) for r in rows],
        "total": total_n, "page": page, "pageSize": page_size,
        "totalPages": (total_n + page_size - 1) // page_size if total_n else 0,
    }


def get_by_id(db: Session, ticket_id: int, *, user_id: int) -> Optional[Dict[str, Any]]:
    row = db.get(ConfirmTicket, ticket_id)
    if row is None or row.deleted:
        return None
    if row.user_id != user_id:
        # 防越权：别人不能看你的票据
        raise PermissionError("ticket not owned by user")
    return _to_dict(row)


# =============================================================
# 用户动作（确认 / 拒绝）—— 5 道防御在此落地
# =============================================================
def confirm(db: Session, ticket_id: int, *, user_id: int) -> Dict[str, Any]:
    """人点确认：校验 → 执行 → 状态推进；执行失败也保留 ticket（标 executed 但 result 带错）"""
    row = db.get(ConfirmTicket, ticket_id)
    if row is None or row.deleted:
        raise LookupError("ticket 不存在")
    if row.user_id != user_id:
        raise PermissionError("ticket not owned by user")
    now = dt.datetime.now()
    if row.status == STATUS_EXECUTED:
        # 防御3·幂等：二次 confirm 返原 result，不重复执行（防双花）
        return _to_dict(row)
    if row.status in (STATUS_REJECTED,):
        raise ValueError("ticket 已被拒绝")
    if row.status == STATUS_EXPIRED:
        raise ValueError("ticket 已过期")
    if row.status != STATUS_PROPOSED:
        raise ValueError(f"非法状态迁移: {row.status} -> confirmed")
    # 防御1·参数冻结：人确认时再算 args_hash，与写入时比对
    if args_hash(row.tool_args) != row.args_hash:
        raise ValueError("ticket 参数在确认时被修改（疑似 AI 篡改），拒绝执行")
    # 防御2·TTL：未过期才允许 confirm
    if now > row.expires_at:
        row.status = STATUS_EXPIRED
        row.update_time = now
        db.commit()
        raise ValueError("ticket 已过期")
    # 状态推进到 confirmed（再 execute）
    row.status = STATUS_CONFIRMED
    row.confirmed_at = now
    row.update_time = now
    db.commit()

    # 执行
    executor = TOOL_EXECUTORS.get(row.tool_name)
    if executor is None:
        # 注册表里没了——业务层下线了工具，票据走完审计但不执行
        result: Dict[str, Any] = {"error": "tool executor not registered", "skipped": True}
        _metrics.record_confirm_ticket_failed()
    else:
        try:
            result = executor(row.tool_args, row.user_id)
            _metrics.record_confirm_ticket_executed(row.tool_name)
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            _metrics.record_confirm_ticket_failed()
            logger.exception(f"confirm_ticket.execute failed id={ticket_id} tool={row.tool_name}")
    row.result_snapshot = result
    row.executed_at = dt.datetime.now()
    row.status = STATUS_EXECUTED
    row.update_time = row.executed_at
    db.commit()
    logger.info(
        f"confirm_ticket.execute id={ticket_id} tool={row.tool_name} "
        f"ok={'error' not in result}"
    )
    return _to_dict(row)


def reject(db: Session, ticket_id: int, *, user_id: int,
           reason: Optional[str] = None) -> Dict[str, Any]:
    row = db.get(ConfirmTicket, ticket_id)
    if row is None or row.deleted:
        raise LookupError("ticket 不存在")
    if row.user_id != user_id:
        raise PermissionError("ticket not owned by user")
    now = dt.datetime.now()
    if row.status != STATUS_PROPOSED:
        raise ValueError(f"非法状态迁移: {row.status} -> rejected")
    row.status = STATUS_REJECTED
    row.rejected_at = now
    row.result_snapshot = {"reason": (reason or "")[:500]}
    row.update_time = now
    db.commit()
    _metrics.record_confirm_ticket_rejected(row.tool_name)
    logger.info(f"confirm_ticket.reject id={ticket_id} tool={row.tool_name} reason={reason!r}")
    return _to_dict(row)


def sweep_expired(db: Session) -> int:
    """后台或 lazy 调用：把超期 proposed 标记 expired（防御2的兜底）"""
    now = dt.datetime.now()
    rows = db.execute(
        select(ConfirmTicket).where(
            ConfirmTicket.status == STATUS_PROPOSED,
            ConfirmTicket.expires_at < now,
            ConfirmTicket.deleted == 0,
        )
    ).scalars().all()
    for r in rows:
        r.status = STATUS_EXPIRED
        r.update_time = now
    db.commit()
    return len(rows)


def _iso(v: Any) -> Optional[str]:
    return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, dt.datetime) else None


def _to_dict(row: ConfirmTicket) -> Dict[str, Any]:
    return {
        "id": row.id,
        "tool_name": row.tool_name,
        "tool_args": row.tool_args,
        "args_hash": row.args_hash,
        "summary": row.summary,
        "status": row.status,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "expires_at": _iso(row.expires_at),
        "confirmed_at": _iso(row.confirmed_at),
        "rejected_at": _iso(row.rejected_at),
        "executed_at": _iso(row.executed_at),
        "result_snapshot": row.result_snapshot,
        "create_time": _iso(row.create_time),
        "update_time": _iso(row.update_time),
    }