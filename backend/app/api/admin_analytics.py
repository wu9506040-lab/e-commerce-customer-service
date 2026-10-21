"""admin 运营聚合指标 API（P4-2）。"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.clients.mysql_client import get_db
from app.models.user import User
from app.schemas.admin_analytics import AdminAnalyticsResponse
from app.services.analytics_service import AnalyticsService
from app.services.audit_service import try_log_action
from app.services import kpi_service  # WP1 业务 KPI

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/analytics",
    tags=["admin-analytics"],
)


def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _user_agent(request: Request) -> Optional[str]:
    user_agent = request.headers.get("user-agent", "")
    return user_agent[:500] if user_agent else None


@router.get(
    "",
    response_model=AdminAnalyticsResponse,
    summary="[admin] 运营聚合指标",
    description=(
        "返回按日会话活跃度、assistant 响应延迟 P50/P95、转人工事件分布，"
        "以及当前进程最近 100 次 RAG 检索的 hit@K。强制时间窗且最大 90 天。"
    ),
)
def get_admin_analytics(
    request: Request,
    start_date: Optional[datetime] = Query(
        None,
        description="起始时间（含），缺省返 400",
    ),
    end_date: Optional[datetime] = Query(
        None,
        description="结束时间（不含），缺省返 400",
    ),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """读取 admin 运营驾驶舱聚合数据。"""
    if start_date is None or end_date is None:
        raise HTTPException(
            status_code=400,
            detail="admin analytics 必须传 start_date + end_date（防全表扫描）",
        )
    if end_date <= start_date:
        raise HTTPException(status_code=400, detail="end_date 必须 > start_date")
    if end_date - start_date > timedelta(days=90):
        raise HTTPException(
            status_code=400,
            detail="时间范围 ≤ 90 天（如需更长请分多次查）",
        )

    result = AnalyticsService.get_analytics(db, start_date, end_date)

    try_log_action(
        user=admin,
        action="admin_get_analytics",
        target_type="analytics",
        target_id=None,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        detail={
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "cache_hit": bool(result.get("cache_hit")),
            "daily_points": len(result.get("daily_activity", [])),
        },
    )
    logger.info(
        f"admin_get_analytics: admin={admin.username}(id={admin.id}) "
        f"window={start_date}~{end_date} cache_hit={result.get('cache_hit')}"
    )
    return result


@router.get("/kpi", summary="业务 KPI 大盘（WP1：deflection/CSAT/拦截率）")
def get_kpi(
    days: int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """客服域业务指标窗口聚合（指标定义见 kpi_service 模块注释）。"""
    end = datetime.now()
    start = end - timedelta(days=days)
    return kpi_service.compute_kpi(db, start, end)


@router.get("/kpi/trend", summary="业务 KPI 按日趋势（自助解决率折线）")
def get_kpi_trend(
    days: int = Query(14, ge=1, le=60),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return kpi_service.compute_trend(db, days=days)


# =============================================================
# V18.3: 数据看板真实统计（/board）——DataBoard.vue 数据源
# 口径说明：
#   渠道       = session_id 前缀（xianyu-* → 闲鱼，其余 → 商城）
#   响应时间   = 同会话内 assistant 消息时间 - 前一条 user 消息时间（秒）
#   AI 回复率  = assistant 消息中 intent_snapshot.ai=true 占比（排除人工）
#   转人工率   = 窗口内建工单数 / 去重会话数
#   议价解决率 = 议价诉求会话中未升级工单的占比
#   测试会话过滤：test_/demo_/debug_/verify 等 known test 前缀不入统计
# =============================================================
import re as _re
import json as _json
from collections import Counter as _Counter, defaultdict as _defaultdict

from app.models.message import Message  # noqa: E402
from app.models.handoff_ticket import HandoffTicket  # noqa: E402

_TEST_SESSION_RE = _re.compile(
    r"^(test|demo_|debug_|verify|unq_|force_|sequ|fresh|final_|robust|snap|v3|m8|full_|xianyu$|dedup)",
    _re.IGNORECASE,
)

_STAGE_KEYWORDS = {
    "议价": ("便宜", "价格", "少点", "优惠", "砍价", "改价", "抹", "打折"),
    "售后": ("退款", "退货", "退钱", "质保", "保修", "维修", "换货", "破损", "投诉", "人工", "专员", "售后"),
    "售中": ("发货", "物流", "快递", "签收", "地址", "发票", "运费", "订单", "催"),
}


def _stage_of(text: str) -> str:
    for stage, kws in _STAGE_KEYWORDS.items():
        if any(k in text for k in kws):
            return stage
    return "售前咨询"


@router.get("/board", summary="数据看板真实统计（DataBoard 数据源）")
def analytics_board(
    days: int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    since = datetime.now() - timedelta(days=days)
    try_log_action(admin, "analytics_board", detail={"days": days})

    msgs = (
        db.query(Message)
        .filter(Message.create_time >= since)
        .order_by(Message.id.asc())
        .all()
    )
    tickets = (
        db.query(HandoffTicket)
        .filter(HandoffTicket.create_time >= since)
        .count()
    )

    # 过滤测试会话
    kept, excluded = [], 0
    for r in msgs:
        if _TEST_SESSION_RE.match(r.session_id or ""):
            excluded += 1
            continue
        kept.append(r)

    # 日期轴（含零天）
    dates = []
    base = datetime.now().date()
    for i in range(days - 1, -1, -1):
        dates.append((base - timedelta(days=i)).strftime("%m/%d"))
    date_key = lambda dt: dt.strftime("%m/%d")  # noqa: E731

    channel_day = _defaultdict(lambda: _Counter())  # date → {xianyu, mall}
    hourly = [0] * 24
    first_seen: dict[str, str] = {}
    session_msgs: dict[str, list] = _defaultdict(list)
    user_texts: list[str] = []
    stage_counter: _Counter = _Counter()
    ai_n, human_n = 0, 0

    for r in kept:
        ct = r.create_time
        if ct is None:
            continue
        d = date_key(ct)
        ch = "xianyu" if (r.session_id or "").startswith("xianyu-") else "mall"
        channel_day[d][ch] += 1
        hourly[ct.hour] += 1
        first_seen.setdefault(r.session_id, d)
        session_msgs[r.session_id].append(r)
        snap = r.intent_snapshot if isinstance(r.intent_snapshot, dict) else {}
        if r.role == "assistant":
            if snap.get("human"):
                human_n += 1
            else:
                ai_n += 1
        elif r.role == "user" and (r.content or "").strip():
            t = (r.content or "").strip()
            user_texts.append(t)
            stage_counter[_stage_of(t)] += 1

    today = date_key(datetime.now())
    today_messages = channel_day[today]["xianyu"] + channel_day[today]["mall"]

    # 响应时间：同会话 assistant − 前一条 user（秒）
    resp_day: dict[str, list[float]] = _defaultdict(list)
    for _sid, rows in session_msgs.items():
        last_user_ts = None
        for r in rows:
            if r.create_time is None:
                continue
            if r.role == "user":
                last_user_ts = r.create_time
            elif r.role == "assistant" and last_user_ts is not None:
                delta = (r.create_time - last_user_ts).total_seconds()
                if 0 < delta < 3600:  # 超过 1 小时视为离线间隔，不计
                    resp_day[date_key(r.create_time)].append(delta)
                last_user_ts = None

    resp_avg = [round(sum(v) / len(v), 1) if v else 0 for v in (resp_day.get(d, []) for d in dates)]
    resp_max = [round(max(v), 1) if v else 0 for v in (resp_day.get(d, []) for d in dates)]
    resp_min = [round(min(v), 1) if v else 0 for v in (resp_day.get(d, []) for d in dates)]
    all_resp = [x for v in resp_day.values() for x in v]

    # 会话级标签 / 议价
    bargain_sessions, after_sale_sessions = set(), set()
    for sid, rows in session_msgs.items():
        for r in rows:
            if r.role != "user":
                continue
            t = r.content or ""
            if any(k in t for k in _STAGE_KEYWORDS["议价"]):
                bargain_sessions.add(sid)
            if any(k in t for k in _STAGE_KEYWORDS["售后"]):
                after_sale_sessions.add(sid)
    bargain_resolved = bargain_sessions - set(
        str(t.session_id) for t in db.query(HandoffTicket).filter(HandoffTicket.create_time >= since).all()
    )
    bargain_rate = round(len(bargain_resolved) / len(bargain_sessions) * 100, 1) if bargain_sessions else 100.0

    sess_count = len(session_msgs)
    ai_rate = round(ai_n / (ai_n + human_n) * 100, 1) if (ai_n + human_n) else 100.0
    handoff_rate = round(tickets / sess_count * 100, 1) if sess_count else 0.0

    # 热门问题 TOP10（买家原文归一计数）
    norm = _Counter()
    _ORDER_NO_RE = _re.compile(r"^(ORD|NO|DD|E)?[0-9A-Za-z]{8,}$")  # 纯订单号类非问题文本
    for t in user_texts:
        t2 = _re.sub(r"[\s？?！!。，,.~]+", "", t)
        if len(t2) < 2:
            continue
        key = t2[:14]
        # 过滤纯订单号/工单号类文本（含 "ORD2026xxxx 帮我查一下" 前缀型）
        if _ORDER_NO_RE.match(t2) or _ORDER_NO_RE.match(key):
            continue
        norm[key] += 1
    top10 = norm.most_common(10)

    # 客户标签
    tag_counter: _Counter = _Counter()
    for sid, rows in session_msgs.items():
        if len(rows) >= 20:
            tag_counter["老客"] += 1
        elif sid in after_sale_sessions:
            tag_counter["售后中"] += 1
        elif sid in bargain_sessions:
            tag_counter["议价中"] += 1
        else:
            tag_counter["售前意向"] += 1

    xianyu_trend = [channel_day[d]["xianyu"] for d in dates]
    mall_trend = [channel_day[d]["mall"] for d in dates]
    new_cust = [sum(1 for v in first_seen.values() if v == d) for d in dates]
    total_xy = sum(xianyu_trend)
    total_mall = sum(mall_trend)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "excluded_test_messages": excluded,
        "kpis": {
            "today_messages": today_messages,
            "avg_response_sec": round(sum(all_resp) / len(all_resp), 1) if all_resp else 0,
            "ai_reply_rate": ai_rate,
            "handoff_rate": handoff_rate,
            "bargain_resolve_rate": bargain_rate,
            "new_customers_today": new_cust[-1],
        },
        "msg_trend": {"dates": dates, "xianyu": xianyu_trend, "mall": mall_trend},
        "channel_split": [
            {"name": "商城", "value": total_mall},
            {"name": "闲鱼", "value": total_xy},
        ],
        "hourly": hourly,
        "resp_trend": {"dates": dates, "avg": resp_avg, "max": resp_max, "min": resp_min},
        "top_questions": [{"q": q, "count": c} for q, c in top10],
        "intent_split": [{"name": k, "value": v} for k, v in stage_counter.most_common()],
        "new_cust_trend": {"dates": dates, "counts": new_cust},
        "customer_tags": [{"name": k, "value": v} for k, v in tag_counter.most_common()],
    }
