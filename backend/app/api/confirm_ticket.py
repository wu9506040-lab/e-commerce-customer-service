"""confirm_ticket API - V14（human-in-the-loop）

GET   /api/confirm/tickets                 当前用户的待确认票据（默认 status=proposed）
GET   /api/confirm/tickets/{id}            单条详情（归属校验——只能看自己的）
POST  /api/confirm/tickets/{id}/confirm    人点确认——服务端校验+执行
POST  /api/confirm/tickets/{id}/reject     人点拒绝——状态置 rejected
POST  /api/confirm/tickets/propose         AI 侧提案入口（业务模块调用，非用户直调；
                                         当前提供 refund_execute 一档演示）
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.clients.mysql_client import get_db
from app.models.user import User
from app.services import confirm_ticket_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/confirm", tags=["confirm_ticket"])


class ConfirmBody(BaseModel):
    """AI 侧提案 body——业务模块把"想做什么"传进来；人不直调"""
    tool_name: str = Field(..., min_length=1, max_length=64)
    args: dict = Field(..., description="工具参数（JSON-canonical 计算 args_hash 冻结）")
    summary: str = Field(..., min_length=1, max_length=500,
                         description="AI 给人的摘要（前端卡片展示）")
    ttl_sec: int = Field(300, ge=30, le=3600, description="过期时长（默认 5 分钟）")
    session_id: str = Field("", max_length=64)


@router.post("/tickets/propose", summary="AI 侧提案（业务模块调用，非用户直调）")
def propose(
    body: ConfirmBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        data = confirm_ticket_service.propose(
            db,
            tool_name=body.tool_name,
            args=body.args,
            summary=body.summary,
            user_id=user.id,
            session_id=body.session_id,
            ttl_sec=body.ttl_sec,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 提案路径返回 202（资源已接收但未最终执行），前端用此状态触发卡片展示
    return {"ok": True, "data": data}


@router.get("/tickets", summary="当前用户的待确认票据列表")
def list_tickets(
    status: Optional[str] = Query(None, pattern="^(proposed|confirmed|executed|rejected|expired)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {"ok": True, "data": confirm_ticket_service.list_for_user(
        db, user_id=user.id, status=status, page=page, page_size=page_size)}


@router.get("/tickets/{ticket_id}", summary="单条票据详情")
def get_ticket(
    ticket_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        data = confirm_ticket_service.get_by_id(db, ticket_id, user_id=user.id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="ticket 不属于当前用户")
    if data is None:
        raise HTTPException(status_code=404, detail="ticket 不存在")
    return {"ok": True, "data": data}


@router.post("/tickets/{ticket_id}/confirm", summary="人点确认——服务端校验+执行")
def confirm_ticket(
    ticket_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        data = confirm_ticket_service.confirm(db, ticket_id, user_id=user.id)
    except LookupError:
        raise HTTPException(status_code=404, detail="ticket 不存在")
    except PermissionError:
        raise HTTPException(status_code=403, detail="ticket 不属于当前用户")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "data": data}


class RejectBody(BaseModel):
    reason: Optional[str] = Field(None, max_length=500)


@router.post("/tickets/{ticket_id}/reject", summary="人点拒绝")
def reject_ticket(
    ticket_id: int,
    body: RejectBody,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        data = confirm_ticket_service.reject(db, ticket_id, user_id=user.id, reason=body.reason)
    except LookupError:
        raise HTTPException(status_code=404, detail="ticket 不存在")
    except PermissionError:
        raise HTTPException(status_code=403, detail="ticket 不属于当前用户")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "data": data}