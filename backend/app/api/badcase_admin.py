"""badcase 池管理 API — V14（2.4 飞轮机制化）的审核面

GET   /api/admin/badcases          候选列表（状态/信号过滤 + 分页）
PATCH /api/admin/badcases/{id}     人工审核：标注 expected_source / 废弃
POST  /api/admin/badcases/collect  低置信信号扫描入池（手动触发；无 cron 的诚实形态）

合入评测集不走 API：`scripts/merge_badcase_to_eval.py` 由人挑时机跑，
稳定键 source 防 doc_id 漂移——"半自动"的自动是采集，半是合入前的人工判断。
"""
import datetime as dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.clients.mysql_client import get_db
from app.models.user import User
from app.services import badcase_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/badcases", tags=["badcase"])


class ReviewBody(BaseModel):
    action: str = Field(..., pattern="^(reviewed|discarded)$")
    expected_source: Optional[str] = Field(None, max_length=200)
    note: Optional[str] = Field(None, max_length=500)


class CollectBody(BaseModel):
    days: int = Field(7, ge=1, le=90, description="扫描最近 N 天的意图快照")
    threshold: float = Field(0.6, gt=0, lt=1, description="LLM 置信度低于此值入池")
    limit: int = Field(200, ge=1, le=1000, description="本次最多入池条数")


@router.get("", summary="badcase 候选列表")
def list_badcases(
    status: Optional[str] = Query(None, pattern="^(new|reviewed|discarded|merged)$"),
    signal: Optional[str] = Query(None, pattern="^(thumbs_down|low_confidence|guard_blocked)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return {"ok": True, "data": badcase_service.list_candidates(
        db, status=status, signal=signal, page=page, page_size=page_size)}


@router.patch("/{badcase_id}", summary="审核 badcase 候选")
def review_badcase(
    badcase_id: int,
    body: ReviewBody,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        data = badcase_service.review(
            db, badcase_id=badcase_id, action=body.action, admin_id=admin.id,
            expected_source=body.expected_source, note=body.note,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="badcase 不存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "data": data}


@router.post("/collect", summary="扫描低置信意图快照入池")
def collect_low_confidence(
    body: CollectBody,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    since = dt.datetime.now() - dt.timedelta(days=body.days)
    try:
        data = badcase_service.collect_low_confidence(
            db, since=since, threshold=body.threshold, limit=body.limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("badcase collect(admin=%s): %s", admin.username, data)
    return {"ok": True, "data": data}
