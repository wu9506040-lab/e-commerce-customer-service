"""回答评价 API — WP1 业务指标（CSAT）的采集入口

POST /api/chat/rate：用户对某条回答 👍/（匿名可用，登录用户记真实 id）。
同一目标重复评价按覆盖更新（幂等），供 kpi_service 聚合 CSAT。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user_optional
from app.clients.mysql_client import get_db
from app.models.user import User
from app.services import kpi_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ratings"])


class RatePayload(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    message_id: int = Field(0, ge=0, description="被评价消息 id；0=会话粒度评价")
    rating: str = Field(..., pattern="^(up|down)$")
    comment: Optional[str] = Field(None, max_length=500)


@router.post("/chat/rate", summary="回答点赞/点踩")
def rate(
    payload: RatePayload,
    user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    user_id = user.id if user is not None else 0
    result = kpi_service.upsert_rating(
        db,
        session_id=payload.session_id,
        user_id=user_id,
        message_id=payload.message_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    return {"ok": result.get("ok", False), "data": result}
