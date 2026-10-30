"""Admin 人工客服工作台 API（V18.2 全渠道版 · Chatwoot 式三栏）

路由前缀 /api/admin/xianyu（保持兼容），全部 require_admin。

数据模型（Shadcn 风格三栏工作台）：
- GET  /conversations?channel=all|xianyu|mall
        统一会话列表：闲鱼（session xianyu-* 前缀 + Redis 实时态）+ 商城（其余会话）
- GET  /conversations/{channel}/{cid}/messages   会话消息历史（含 AI/人工标记）
- GET  /events?limit=    实时事件流（daemon 上行）
- POST /send             人工直发（闲鱼=落库+daemon 下发；商城=仅落库，买家端轮询拉取）
- POST /takeover|release 接管/恢复 AI

AI/人工区分：Message.intent_snapshot.human === true → 人工；否则 AI。
"""
import json
import logging

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.clients.mysql_client import get_db
from app.core.config import settings
from app.models.message import Message
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/xianyu", tags=["admin-xianyu"])

COMMAND_CHANNEL = "xianyu:commands"
WORKERS_KEY = "xianyu:workers"  # V18.2 多账号状态（daemon 上报）
EVENTS_KEY = "xianyu:events"
SESSION_PREFIX = "xianyu-"


def _redis() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _cid_to_session(cid: str) -> str:
    cid = str(cid).strip()
    return cid if cid.startswith(SESSION_PREFIX) else f"{SESSION_PREFIX}{cid}"


def _session_to_cid(session_id: str) -> str:
    return session_id[len(SESSION_PREFIX):] if session_id.startswith(SESSION_PREFIX) else session_id


def _msg_row(m: Message) -> dict:
    snapshot = m.intent_snapshot if isinstance(m.intent_snapshot, dict) else {}
    is_human = snapshot.get("human") is True
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "create_time": str(m.create_time),
        # V18.2: 回复者标识——human=人工坐席，assistant=AI
        "sender": "human" if is_human else ("assistant" if m.role == "assistant" else "buyer"),
        "intent_snapshot": snapshot,
    }


# =============================================================
# 统一会话列表（多渠道）
# =============================================================

def _agg_sessions(db: Session, prefix_like: str, limit: int):
    rows = (
        db.query(
            Message.session_id,
            Message.account,
            func.count(Message.id).label("msg_count"),
            func.max(Message.id).label("last_id"),
            func.max(Message.create_time).label("last_time"),
        )
        .filter(Message.session_id.like(prefix_like))
        .group_by(Message.session_id, Message.account)
        .order_by(func.max(Message.id).desc())
        .limit(limit)
        .all()
    )
    out = []
    for r in rows:
        out.append({
            "session_id": r.session_id,
            "account": r.account or "a1",
            "msg_count": int(r.msg_count or 0),
            "last_time": str(r.last_time) if r.last_time else None,
            "last_msg_id": int(r.last_id) if r.last_id else 0,
        })
    # 批量取最后一条消息内容
    last_ids = [x["last_msg_id"] for x in out if x["last_msg_id"]]
    last_msgs: dict[int, Message] = {}
    if last_ids:
        for m in db.query(Message).filter(Message.id.in_(last_ids)).all():
            last_msgs[m.id] = m
    for x in out:
        m = last_msgs.get(x["last_msg_id"])
        x["last_role"] = m.role if m else None
        x["last_text"] = (m.content or "")[:120] if m else None
        del x["last_msg_id"]
    return out


@router.get("/conversations", summary="全渠道会话列表（闲鱼+商城）")
def list_conversations(
    channel: str = Query("all", pattern="^(all|xianyu|mall)$"),
    limit: int = Query(40, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    convs: list[dict] = []

    if channel in ("all", "xianyu"):
        for x in _agg_sessions(db, f"{SESSION_PREFIX}%", limit):
            convs.append({
                "channel": "xianyu",
                "cid": _session_to_cid(x["session_id"]),
                "session_id": x["session_id"],
                "name": _session_to_cid(x["session_id"]),
                "account": x.get("account", "a1"),
                "msg_count": x["msg_count"],
                "last_time": x["last_time"],
                "last_role": x["last_role"],
                "last_text": x["last_text"],
            })

    if channel in ("all", "mall"):
        # 商城会话：session_id 无 xianyu 前缀；显示名 join users.username
        rows = (
            db.query(
                Message.session_id,
                Message.user_id,
                func.count(Message.id).label("msg_count"),
                func.max(Message.id).label("last_id"),
                func.max(Message.create_time).label("last_time"),
            )
            .filter(~Message.session_id.like(f"{SESSION_PREFIX}%"))
            .group_by(Message.session_id, Message.user_id)
            .order_by(func.max(Message.id).desc())
            .limit(limit)
            .all()
        )
        last_ids = [int(r.last_id) for r in rows if r.last_id]
        last_msgs: dict[int, Message] = {}
        if last_ids:
            for m in db.query(Message).filter(Message.id.in_(last_ids)).all():
                last_msgs[m.id] = m
        user_ids = {int(r.user_id) for r in rows if r.user_id}
        user_names: dict[int, str] = {}
        if user_ids:
            from app.models.user import User as U
            for u in db.query(U).filter(U.id.in_(user_ids)).all():
                user_names[u.id] = u.username or (u.email or "").split("@")[0]
        for r in rows:
            sid = r.session_id
            m = last_msgs.get(int(r.last_id)) if r.last_id else None
            uid = int(r.user_id)
            # user_id=0 是匿名访客会话（商城测试流量），显示名用会话尾号区分
            name = user_names.get(uid) or (f"访客 {sid[-6:]}" if uid == 0 else f"用户{uid}")
            convs.append({
                "channel": "mall",
                "cid": sid,
                "session_id": sid,
                "name": name,
                "msg_count": int(r.msg_count or 0),
                "last_time": str(r.last_time) if r.last_time else None,
                "last_role": m.role if m else None,
                "last_text": (m.content or "")[:120] if m else None,
            })

    convs.sort(key=lambda c: c["last_time"] or "", reverse=True)

    # 实时态叠加（接管中标记 + 最新事件时间）
    r = _redis()
    events = []
    try:
        events = [json.loads(x) for x in r.lrange(EVENTS_KEY, 0, 199)]
    except Exception:
        pass
    for c in convs:
        if c["channel"] != "xianyu":
            continue
        takeover = False
        for ev in events:
            if ev.get("data", {}).get("cid") == c["cid"]:
                if ev.get("kind") == "takeover":
                    takeover = True
                elif ev.get("kind") == "release":
                    takeover = False
        c["takeover"] = takeover
    return {"conversations": convs, "total": len(convs)}


@router.get("/conversations/{channel}/{cid}/messages", summary="会话消息历史")
def get_messages(
    channel: str,
    cid: str,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    sid = _cid_to_session(cid) if channel == "xianyu" else cid
    msgs = (
        db.query(Message)
        .filter(Message.session_id == sid)
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "channel": channel,
        "cid": cid,
        "messages": [_msg_row(m) for m in reversed(msgs)],
    }


@router.get("/events", summary="实时事件流（daemon 上行）")
def get_events(
    limit: int = Query(50, ge=1, le=200),
    _admin: User = Depends(require_admin),
):
    r = _redis()
    try:
        raw = r.lrange(EVENTS_KEY, 0, limit - 1)
        return {"events": [json.loads(x) for x in raw]}
    except Exception as e:
        return {"events": [], "error": str(e)}


# =============================================================
# V18.2 多账号管理（账号注册表在 daemon 侧，状态经 Redis 上报）
# =============================================================

@router.get("/accounts", summary="账号列表（含连接状态）")
def list_accounts(_admin: User = Depends(require_admin)):
    r = _redis()
    workers = r.hgetall(WORKERS_KEY)
    out = []
    for aid, raw in workers.items():
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    out.sort(key=lambda x: x.get("id", ""))
    return {"accounts": out, "total": len(out)}


@router.post("/accounts/{account_id}/{action}", summary="账号启停/重启/重载")
def account_action(
    account_id: str,
    action: str,
    _admin: User = Depends(require_admin),
):
    if action not in ("start", "stop", "restart", "reload"):
        raise HTTPException(status_code=400, detail="非法操作")
    payload = {"action": f"account_{action}", "account_id": account_id}
    _redis().publish(COMMAND_CHANNEL, json.dumps(payload, ensure_ascii=False))
    return {"ok": True, "action": action, "account_id": account_id}


# =============================================================
# V18.2.3: 外发通知（钉钉/企微/飞书/微信推送），配置文件 config/notify.json
# =============================================================

@router.get("/notify/config", summary="外发通知配置（敏感字段脱敏）")
def get_notify_config(_admin: User = Depends(require_admin)):
    from app.services.xianyu import notification_service as ns
    cfg = ns.load_config()
    masked = json.dumps(cfg, ensure_ascii=False)
    import re as _re
    for token in ("access_token=", "key=", "hook/", "AT_", "SEC"):
        masked = _re.sub(token + r"[^\"\\s,}]+", token + "***", masked)
    return json.loads(masked)


@router.post("/notify/config", summary="保存外发通知配置")
def save_notify_config(cfg: dict, _admin: User = Depends(require_admin)):
    from app.services.xianyu import notification_service as ns
    ns.save_config(cfg)
    return {"ok": True}


@router.post("/notify/test", summary="发送测试通知（推送到所有启用通道）")
def test_notify(_admin: User = Depends(require_admin)):
    from app.services.xianyu import notification_service as ns
    results = ns.push_notification(
        "【测试】智能客服管理台",
        "这是一条测试通知：工单/退款/转人工事件会推送到这里。",
        force=True,
    )
    return {"ok": True, "results": results}


@router.get("/tickets", summary="待处理工单（工作台通知用）")
def list_open_tickets(
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """工作台顶栏通知：pending/taken 工单计数 + 最新 5 张"""
    from app.models.handoff_ticket import HandoffTicket
    open_rows = (
        db.query(HandoffTicket)
        .filter(HandoffTicket.status.in_(["pending", "taken"]))
        .order_by(HandoffTicket.id.desc())
        .all()
    )
    latest = open_rows[:limit]
    return {
        "open_count": len(open_rows),
        "p0_count": sum(1 for t in open_rows if t.priority == "P0"),
        "tickets": [
            {
                "id": t.id,
                "ticket_no": t.ticket_no,
                "session_id": t.session_id,
                "reason": t.reason,
                "priority": t.priority,
                "status": t.status,
                "summary_text": (t.summary or {}).get("summary_text", "") if isinstance(t.summary, dict) else str(t.summary or "")[:120],
                "create_time": str(t.create_time),
            }
            for t in latest
        ],
    }


class SendBody(BaseModel):
    cid: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=2000)
    toid: str = Field("", description="买家 unb（可选，daemon 有映射缓存）")
    channel: str = Field("xianyu", description="xianyu=直发+daemon 下发；mall=仅落库（买家端轮询拉取）")


class TakeoverBody(BaseModel):
    cid: str = Field(..., min_length=1)


@router.post("/send", summary="人工直接发消息（闲鱼渠道；落库标记 human）")
def send_xianyu_message(
    body: SendBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    # 1. 落库（role=assistant + intent_snapshot.human=true → 工作台可区分 AI/人工）
    # V19.1：channel 显式区分——闲鱼 cid 到这里已被剥前缀，不能再用前缀判断渠道
    if body.channel == "mall":
        row = Message(
            session_id=body.cid,  # 商城会话：cid 即 session_id，禁止加 xianyu- 前缀
            user_id=admin.id,
            role="assistant",
            content=body.text,
            intent_snapshot={"human": True, "agent": admin.username},
        )
        db.add(row)
        db.commit()
        logger.info(f"[mall-direct] 人工直发落库（商城渠道）: sid={body.cid} admin={admin.username}")
        return {"ok": True, "queued": False, "channel": "mall", "cid": body.cid, "message_id": row.id}

    sid = _cid_to_session(body.cid)
    row = Message(
        session_id=sid,
        user_id=admin.id,
        role="assistant",
        content=body.text,
        intent_snapshot={"human": True, "agent": admin.username},
    )
    db.add(row)
    db.commit()
    # 1.5 反查买家 toid：daemon 的 cid→unb 映射在内存里（重启即丢），
    # 这里从 MySQL 兜底——买家消息落库时 user_id = int(unb)
    toid = body.toid
    if not toid:
        m = (
            db.query(Message)
            .filter(Message.session_id == sid, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )
        if m is not None and str(m.user_id).isdigit():
            toid = str(m.user_id)

    # 2. 下发命令（daemon ws 通道，无限重试）
    r = _redis()
    payload = json.dumps(
        {"action": "send", "cid": body.cid, "toid": toid, "text": body.text},
        ensure_ascii=False,
    )
    r.publish(COMMAND_CHANNEL, payload)
    return {"ok": True, "queued": True, "cid": body.cid, "toid": toid, "message_id": row.id}


@router.post("/takeover", summary="接管会话（AI 停止自动回复）")
def takeover_xianyu(body: TakeoverBody, _admin: User = Depends(require_admin)):
    _redis().publish(
        COMMAND_CHANNEL,
        json.dumps({"action": "takeover", "cid": body.cid}, ensure_ascii=False),
    )
    return {"ok": True, "cid": body.cid, "takeover": True}


@router.post("/release", summary="恢复 AI 自动回复")
def release_xianyu(body: TakeoverBody, _admin: User = Depends(require_admin)):
    _redis().publish(
        COMMAND_CHANNEL,
        json.dumps({"action": "release", "cid": body.cid}, ensure_ascii=False),
    )
    return {"ok": True, "cid": body.cid, "takeover": False}
