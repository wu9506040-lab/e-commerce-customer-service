"""
Auth 业务逻辑层

按 §6 规则：services/ 业务编排，调用 core/security 和 models/user
被 api/auth.py 和 api/deps.py 调用
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.user import User


# =============================================================
# 注册
# =============================================================
def register_user(
    db: Session,
    username: str,
    password: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    role: str = "user",
) -> User:
    """
    注册新用户
    :raises ValueError: 用户名已存在
    """
    # 唯一性预检（DB UNIQUE 也会兜底，这里给友好错误）
    existing = db.execute(
        select(User).where(User.username == username, User.deleted == 0)
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"用户名已存在: {username}")

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        email=email,
        role=role,
        status=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# =============================================================
# 认证
# =============================================================
def authenticate(db: Session, username: str, password: str) -> Optional[User]:
    """
    验证明文密码，返回 User 或 None
    - 用户不存在 → None
    - 已禁用 (status=0) → None
    - 占位密码（首次部署的 admin 未设置真密码）→ None
    - 密码错误 → None
    """
    user = db.execute(
        select(User).where(User.username == username, User.deleted == 0)
    ).scalar_one_or_none()

    if user is None:
        return None
    if user.status != 1:
        return None
    if user.password_hash == "__SET_VIA_AUTH_MODULE__":
        # 占位密码不允许登录，提示通过 /auth/change-password 设置
        return None
    if not verify_password(password, user.password_hash):
        return None

    return user


# =============================================================
# 查询
# =============================================================
def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    return db.execute(
        select(User).where(User.id == user_id, User.deleted == 0)
    ).scalar_one_or_none()


def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.execute(
        select(User).where(User.username == username, User.deleted == 0)
    ).scalar_one_or_none()


# =============================================================
# 修改密码
# =============================================================
def change_password(db: Session, user: User, old_password: str, new_password: str) -> None:
    """
    修改密码
    :raises ValueError: 旧密码错误
    """
    if not verify_password(old_password, user.password_hash):
        raise ValueError("旧密码错误")
    user.password_hash = hash_password(new_password)
    db.commit()
