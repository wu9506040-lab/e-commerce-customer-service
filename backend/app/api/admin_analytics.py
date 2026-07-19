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
