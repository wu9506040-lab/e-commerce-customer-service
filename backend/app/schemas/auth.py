"""
Auth 相关 Pydantic Schema

接口:
    POST /auth/register        注册
    POST /auth/login           登录（OAuth2 form，返回 JWT + Set-Cookie）
    POST /auth/logout          登出
    GET  /auth/me              当前用户
    POST /auth/change-password 修改密码
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# =============================================================
# Request
# =============================================================
class RegisterRequest(BaseModel):
    """注册请求"""
    username: str = Field(
        ...,
        min_length=3,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_]+$",
        description="登录名（字母数字下划线）",
    )
    password: str = Field(..., min_length=8, max_length=128, description="密码（≥ 8 字符）")
    display_name: Optional[str] = Field(None, max_length=200, description="显示名")
    email: Optional[str] = Field(
        None,
        max_length=200,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        description="邮箱（可选）",
    )


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., min_length=8, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


# =============================================================
# Response
# =============================================================
class UserOut(BaseModel):
    """用户信息响应（不含密码 hash）"""
    id: int
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    status: int
    create_time: datetime

    model_config = {"from_attributes": True}  # 支持 ORM mode (pydantic v2)


class UserOutStats(UserOut):
    """用户信息 + 统计（/me 专用，承载用户活跃数据）"""
    message_count: int = Field(0, description="该用户未删除消息总数")
    conversation_count: int = Field(0, description="该用户未删除会话总数")


class LoginResponse(BaseModel):
    """登录响应（cookie 自动带 token，body 也带 user 信息方便前端用）"""
    user: UserOut
    message: str = "登录成功"
