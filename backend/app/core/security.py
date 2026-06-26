"""
密码哈希 + JWT 签发/校验

按 §6 规则：core/ 层核心能力，被 services/auth_service 和 api/deps 调用
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings


# =============================================================
# 密码哈希（bcrypt）
# =============================================================
def hash_password(plain: str) -> str:
    """bcrypt 哈希密码，返回 hash 字符串"""
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """验证明文密码 vs hash"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# =============================================================
# JWT 签发与校验
# =============================================================
def create_access_token(subject: str | int, extra: Optional[dict] = None) -> str:
    """
    签发 JWT access token
    :param subject: 用户唯一标识（user_id），放 sub claim
    :param extra: 额外 claims（如 role）
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": now,
        "exp": expire,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """
    解码 JWT，返回 payload dict；失败返回 None
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None
