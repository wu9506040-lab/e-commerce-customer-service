"""Admin 坐席工作台 API - 人工介入工单排队/认领/回复结单

路由前缀 /api/admin/handoff，全部 require_admin（非 admin 403）。

端点：
- GET  /tickets              队列（?status=&page=&page_size=，P0 优先）
- GET  /tickets/{id}         详情 + 会话最近 20 条
- POST /tickets/{id}/take    认领（pending→taken）
- POST /tickets/{id}/resolve 回复并结单（人工回复注入会话，用户下次拉历史可见）
- POST /tickets/{id}/close   直接关闭归档（误触发/用户离开）

M15 人工介入闭环的读侧；写侧在 EscalationService.handoff() 自动落工单。
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.clients.mysql_client import get_db
from app.models.user import User
from app.services import handoff_ticket_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/handoff",
    tags=["admin-handoff"],
)


class ResolveBody(BaseModel):
    reply: str = Field(..., min_length=1, max_length=2000, description="人工回复内容")


@router.get("/tickets", summary="转人工工单队列")
def list_tickets(
    status: Optional[str] = Query(None, description="按状态过滤：pending/taken/resolved/closed"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return svc.list_tickets(db, status=status, page=page, page_size=page_size)


@router.get("/tickets/{ticket_id}", summary="工单详情 + 会话上下文")
def get_ticket(
    ticket_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    detail = svc.get_ticket_detail(db, ticket_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return detail


@router.post("/tickets/{ticket_id}/take", summary="认领工单")
def take_ticket(
    ticket_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = svc.take_ticket(db, ticket_id, admin.username)
    if not result.get("ok"):
        raise HTTPException(status_code=result.get("code", 400), detail=result.get("error"))
    return result


@router.post("/tickets/{ticket_id}/resolve", summary="人工回复并结单")
def resolve_ticket(
    body: ResolveBody,
    ticket_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = svc.resolve_ticket(db, ticket_id, admin.username, body.reply)
    if not result.get("ok"):
        raise HTTPException(status_code=result.get("code", 400), detail=result.get("error"))
    return result


@router.post("/tickets/{ticket_id}/close", summary="关闭工单（不回复）")
def close_ticket(
    ticket_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = svc.close_ticket(db, ticket_id, admin.username)
    if not result.get("ok"):
        raise HTTPException(status_code=result.get("code", 400), detail=result.get("error"))
    return result
