"""
API 层 Depends - get_current_user / require_admin

按 §6 规则：api/ 层依赖注入，把 User 注入到 endpoint 签名里
"""
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.clients.mysql_client import get_db
from app.core.config import settings
from app.core.security import decode_access_token
from app.models.user import User
from app.services.auth_service import get_user_by_id


def _extract_token(request: Request) -> Optional[str]:
    """从 Cookie 取 JWT token；缺失返回 None"""
    return request.cookies.get(settings.COOKIE_NAME)


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    获取当前登录用户（可选）
    未登录 / token 无效 / 用户不存在 / 已禁用 → 返回 None
    """
    token = _extract_token(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        return None
    user = get_user_by_id(db, user_id_int)
    if user is None or user.status != 1:
        return None
    return user


def get_current_user(
    user: Optional[User] = Depends(get_current_user_optional),
) -> User:
    """获取当前登录用户（必填，未登录 401）"""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
        )
    return user


def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """要求 admin 角色（非 admin 返回 403）"""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return user
