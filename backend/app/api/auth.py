"""
Auth HTTP API

接口:
    POST /auth/register        注册（公开）
    POST /auth/login           登录（OAuth2 form，返回 JWT + Set-Cookie）
    POST /auth/logout          登出（清 cookie）
    GET  /auth/me              当前用户（含 stats：message_count / conversation_count）
    POST /auth/change-password 修改密码（已登录）
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.clients.mysql_client import get_db
from app.core.config import settings
from app.core.security import create_access_token
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginResponse,
    RegisterRequest,
    UserOut,
    UserOutStats,
)
from app.services.auth_service import (
    authenticate,
    change_password as svc_change_password,
    register_user,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_token_cookie(response: Response, token: str) -> None:
    """设置 httpOnly Cookie"""
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        max_age=settings.COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/",
    )


# =============================================================
# 注册（公开）
# =============================================================
@router.post("/register", response_model=UserOut, status_code=201)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    try:
        user = register_user(
            db,
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
            email=payload.email,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return UserOut.model_validate(user)


# =============================================================
# 登录（OAuth2 form，方便 Swagger Authorize）
# =============================================================
@router.post("/login", response_model=LoginResponse)
def login(
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = authenticate(db, form_data.username, form_data.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token(subject=user.id, extra={"role": user.role})
    _set_token_cookie(response, token)

    return LoginResponse(user=UserOut.model_validate(user))


# =============================================================
# 登出
# =============================================================
@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key=settings.COOKIE_NAME, path="/")
    return {"message": "已登出"}


# =============================================================
# 当前用户（含 stats）
# =============================================================
@router.get("/me", response_model=UserOutStats)
def me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    返回当前用户 + 统计字段
    - message_count：当前用户未删除消息总数
    - conversation_count：当前用户未删除会话总数
    """
    # 2 条独立 COUNT（user_id 已有索引，单查毫秒级）
    msg_count = db.execute(
        select(func.count(Message.id)).where(
            Message.user_id == current_user.id,
            Message.deleted == 0,
        )
    ).scalar() or 0
    conv_count = db.execute(
        select(func.count(Conversation.id)).where(
            Conversation.user_id == current_user.id,
            Conversation.deleted == 0,
        )
    ).scalar() or 0

    return UserOutStats(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        email=current_user.email,
        role=current_user.role,
        status=current_user.status,
        create_time=current_user.create_time,
        message_count=msg_count,
        conversation_count=conv_count,
    )


# =============================================================
# 修改密码
# =============================================================
@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        svc_change_password(
            db,
            current_user,
            payload.old_password,
            payload.new_password,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "密码已修改"}
