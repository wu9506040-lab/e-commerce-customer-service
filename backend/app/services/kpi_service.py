"""WP1 业务指标（客服域 KPI）计算 — 电商公司看客服系统只看这几个数

指标定义（每个都是行业通用口径，面试可直接讲）：
- deflection_rate  AI 自助解决率：窗口内未产生转人工工单的会话占比（核心降本指标）
- handoff_rate     转人工率 = 1 - deflection（口径互补）
- csat             满意度 = 👍 / (👍+👎)（仅统计有评价的会话）
- rating_coverage  评价覆盖率 = 有评价会话 / 总会话（CSAT 可信度分母，低覆盖的 CSAT 不能吹）
- guard_block_rate 拦截率 = guard 拦截事件 / 会话数（风控有效性）
- avg_rounds       平均解决轮次 = 用户消息数 / 会话数（效率指标）
- p0_ratio         P0 高优占比（业务风险浓度）

数据来源全部真实落库：messages（会话/轮次）、handoff_tickets（转人工）、
message_ratings（评价）、operation_log chat_guard_blocked（拦截）。
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.handoff_ticket import HandoffTicket
from app.models.message import Message
from app.models.message_rating import MessageRating
from app.models.operation_log import OperationLog

# LLM 计价（元/1K tokens）——默认对齐 qwen-plus 公开价，可用 env 覆盖，价格随官方调整
# ⚠️ 成本为「估算」：token 优先取消息真实 token_count，缺失时按中文 1字≈1.5token 折算，非账单级精度
_PRICE_IN_PER_1K = float(os.getenv("LLM_PRICE_IN_PER_1K", "0.0008"))
_PRICE_OUT_PER_1K = float(os.getenv("LLM_PRICE_OUT_PER_1K", "0.002"))
_CHAR_PER_TOKEN = 1.5  # 中文经验比，仅用于无 token_count 的估算


def _safe_div(a: float, b: float) -> Optional[float]:
    return round(a / b, 4) if b else None


def _estimate_tokens(text_len: int, token_count: Optional[int]) -> int:
    """token 估算：真实 token_count 优先，缺失按中文 1字≈1.5token 折算。"""
    if token_count and token_count > 0:
        return int(token_count)
    return int((text_len or 0) / _CHAR_PER_TOKEN) + 1


def _compute_cost(db: Session, start: dt.datetime, end: dt.datetime,
                  sessions_total: int, guard_blocked: int) -> Dict:
    """窗口内 LLM 成本估算（老板视角账单）。

    口径诚实性：
    - prompt tokens = user 消息；completion tokens = assistant 消息（system 固定开销并入 prompt）
    - token_count 列有值用真值，否则字符数/1.5 折算 → 结果是「估算」不是账单，note 字段标注
    - Guard 省钱估算 = 拦截次数 × 平均单会话成本（被拦请求若放行将产生的 LLM 开销）
    """
    in_win = (Message.create_time >= start, Message.create_time < end, Message.deleted == 0)
    in_tokens = out_tokens = 0
    rows = db.execute(
        select(Message.role, func.coalesce(func.sum(Message.token_count), 0),
               func.coalesce(func.sum(func.length(Message.content)), 0))
        .where(*in_win).group_by(Message.role)
    ).all()
    for role, tok_sum, char_sum in rows:
        # 聚合里 token_count 可能混 NULL：用 (真值合计 + 缺失按字符) 的保守近似
        est = int(tok_sum) if tok_sum else int(char_sum / _CHAR_PER_TOKEN)
        if est == 0:
            est = int(char_sum / _CHAR_PER_TOKEN)
        if role == "assistant":
            out_tokens += est
        else:
            in_tokens += est
    cost_in = in_tokens / 1000 * _PRICE_IN_PER_1K
    cost_out = out_tokens / 1000 * _PRICE_OUT_PER_1K
    total = round(cost_in + cost_out, 4)
    avg_per_session = _safe_div(total, sessions_total)
    return {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "total_cost_cny": total,
        "avg_cost_per_session_cny": avg_per_session,
        "guard_savings_cny": round(guard_blocked * (avg_per_session or 0), 4) if guard_blocked else 0.0,
        "pricing": {"model": "qwen-plus", "in_per_1k": _PRICE_IN_PER_1K, "out_per_1k": _PRICE_OUT_PER_1K},
        "note": "估算口径（token 真值优先、缺失按 1字≈0.67token 折算），非账单精度",
    }


def upsert_rating(
    db: Session, *, session_id: str, user_id: int,
    message_id: int, rating: str, comment: Optional[str] = None,
) -> Dict:
    """写入/更新一条回答评价（同目标重复评价覆盖，幂等）。"""
    if rating not in ("up", "down"):
        return {"ok": False, "error": "rating 必须为 up 或 down"}
    row = db.execute(
        select(MessageRating).where(
            MessageRating.session_id == session_id,
            MessageRating.message_id == message_id,
            MessageRating.user_id == user_id,
            MessageRating.deleted == 0,
        )
    ).scalar_one_or_none()
    if row is None:
        row = MessageRating(
            session_id=session_id, message_id=message_id,
            user_id=user_id, rating=rating, comment=comment,
        )
        # 显式写本地时间，避免 SQLite CURRENT_TIMESTAMP(UTC) 与 datetime.now()(本地) 8h 偏差
        row.create_time = dt.datetime.now()
        row.update_time = dt.datetime.now()
        db.add(row)
    else:
        row.rating = rating
        row.comment = comment
    db.commit()
    return {"ok": True, "rating": rating, "session_id": session_id}


def compute_kpi(db: Session, start: dt.datetime, end: dt.datetime) -> Dict:
    """按时间窗聚合客服域业务 KPI。"""
    # 1. 会话与轮次（以 messages 表为准，窗口内有用户消息的会话才算"发生过的会话"）
    in_win = (Message.create_time >= start, Message.create_time < end, Message.deleted == 0)
    sessions_total = db.execute(
        select(func.count(func.distinct(Message.session_id))).where(*in_win)
    ).scalar() or 0
    user_msgs = db.execute(
        select(func.count()).where(*in_win, Message.role == "user")
    ).scalar() or 0

    # 2. 转人工（M15 工单表，按创建时间）
    tw = (HandoffTicket.create_time >= start, HandoffTicket.create_time < end,
          HandoffTicket.deleted == 0)
    handoff_sessions = db.execute(
        select(func.count(func.distinct(HandoffTicket.session_id))).where(*tw)
    ).scalar() or 0
    handoff_total = db.execute(
        select(func.count()).where(*tw)
    ).scalar() or 0
    p0_total = db.execute(
        select(func.count()).where(*tw, HandoffTicket.priority == "P0")
    ).scalar() or 0

    # 3. CSAT（评价）
    rw = (MessageRating.create_time >= start, MessageRating.create_time < end,
          MessageRating.deleted == 0)
    up = db.execute(select(func.count()).where(*rw, MessageRating.rating == "up")).scalar() or 0
    down = db.execute(select(func.count()).where(*rw, MessageRating.rating == "down")).scalar() or 0
    rated_sessions = db.execute(
        select(func.count(func.distinct(MessageRating.session_id))).where(*rw)
    ).scalar() or 0

    # 4. Guard 拦截事件数
    guard_blocked = db.execute(
        select(func.count()).where(
            OperationLog.create_time >= start, OperationLog.create_time < end,
            OperationLog.deleted == 0,
            OperationLog.action == "chat_guard_blocked",
        )
    ).scalar() or 0

    # 5. KPI 合成（分母为 0 的指标返回 None，前端/汇报不得用 0 冒充）
    kpi: Dict = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "sessions_total": sessions_total,
        "messages_user": user_msgs,
        "handoff": {
            "sessions": handoff_sessions,
            "tickets_total": handoff_total,
            "p0_total": p0_total,
        },
        "deflection_rate": _safe_div(sessions_total - handoff_sessions, sessions_total),
        "handoff_rate": _safe_div(handoff_sessions, sessions_total),
        "csat": _safe_div(up, up + down),
        "ratings": {"up": up, "down": down, "rated_sessions": rated_sessions},
        "rating_coverage": _safe_div(rated_sessions, sessions_total),
        "guard_blocked": guard_blocked,
        "guard_block_rate": _safe_div(guard_blocked, sessions_total),
        "avg_rounds": _safe_div(user_msgs, sessions_total),
        "p0_ratio": _safe_div(p0_total, handoff_total),
        "cost": _compute_cost(db, start, end, sessions_total, guard_blocked),
    }
    return kpi


def compute_trend(db: Session, days: int = 14) -> Dict:
    """按日 deflection/handoff/sessions 趋势（大盘折线数据源）。"""
    end = dt.datetime.now()
    # 含今天：start 回溯 days-1 天 → series 最后一天为今天
    start = (end - dt.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_keys = [(start + dt.timedelta(days=i)).strftime("%m-%d") for i in range(days)]
    per_day: Dict[str, Dict[str, int]] = {
        k: {"sessions": 0, "handoffs": 0} for k in day_keys
    }
    # sessions per day（按用户消息出现的自然日去重会话）
    rows = db.execute(
        select(func.date(Message.create_time).label("d"),
               func.count(func.distinct(Message.session_id)))
        .where(Message.create_time >= start, Message.deleted == 0, Message.role == "user")
        .group_by("d")
    ).all()
    for d, cnt in rows:
        key = _d(d)
        if key in per_day:
            per_day[key]["sessions"] = cnt
    rows = db.execute(
        select(func.date(HandoffTicket.create_time), func.count(func.distinct(HandoffTicket.session_id)))
        .where(HandoffTicket.create_time >= start, HandoffTicket.deleted == 0)
        .group_by(func.date(HandoffTicket.create_time))
    ).all()
    for d, cnt in rows:
        key = _d(d)
        if key in per_day:
            per_day[key]["handoffs"] = cnt
    series = [
        {
            "date": k,
            "sessions": v["sessions"],
            "handoffs": v["handoffs"],
            "deflection_rate": _safe_div(v["sessions"] - v["handoffs"], v["sessions"]),
        }
        for k, v in per_day.items()
    ]
    return {"days": days, "series": series}


def _d(d) -> str:
    """date/datetime → 'MM-DD' 键。"""
    s = d if isinstance(d, str) else d.strftime("%Y-%m-%d")
    return s[5:].replace("-", "-")
