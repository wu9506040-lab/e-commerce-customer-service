"""
Public HTTP API - 无需鉴权的公开接口

专门用于云平台公开 demo（避免访客手动注册）：
    POST /api/public/demo-account   一键创建 visitor 账号 + 自动登录
    GET  /api/public/status          公开状态页（uptime / version）

注意：
- 这两个接口对外暴露，生产环境建议加 IP 白名单或域名 CSP 限制
- ENABLE_DEMO_LOGIN=false 时关闭（settings 开关）
- visitor 账号用户名格式：visitor_<uuid8>，密码随机不可登录（防被冒用）
"""
import logging
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.auth import _set_token_cookie
from app.clients.mysql_client import get_db
from app.core.config import settings
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.auth import LoginResponse, UserOut
from app.services.auth_service import register_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/public", tags=["public"])


def _cleanup_expired_demo_users(db: Session) -> int:
    """
    清理过期 demo 账号（避免数据库无限增长）

    清理规则：
    - username 以 'visitor_' 开头
    - create_time 距今超过 30 天
    - 删除前 1% 采样软删（user.deleted=1），保留可观察性
    """
    cutoff = datetime.utcnow() - timedelta(days=30)
    expired = (
        db.query(User)
        .filter(User.username.like("visitor_%"), User.create_time < cutoff)
        .limit(100)
        .all()
    )
    for u in expired:
        u.deleted = 1
    db.commit()
    return len(expired)


@router.post("/demo-account", response_model=LoginResponse)
def create_demo_account(
    response: Response,
    db: Session = Depends(get_db),
):
    """
    一键创建访客 demo 账号 + 自动登录（公开 demo 站点入口）

    流程：
    1. 生成 visitor_<uuid8> 用户名 + 16 字符随机密码
    2. 创建用户（注册流程）
    3. 发 JWT token + Set-Cookie
    4. 异步触发清理过期 visitor（best-effort）

    安全：
    - 密码不可登录（用户从未告知密码）
    - 30 天后自动软删
    - 与正式用户完全隔离
    """
    if not settings.ENABLE_DEMO_LOGIN:
        raise HTTPException(
            status_code=403,
            detail="Demo 账号功能未启用",
        )

    # 1. 生成唯一用户名（带 UUID 后缀防冲突）
    username = f"visitor_{uuid.uuid4().hex[:8]}"
    # 16 字符随机密码，用户不可登录 + 防被冒用
    password = secrets.token_urlsafe(16)

    # 2. 注册用户（role="visitor"：体验账号，下单开放但禁止支付/发货/签收/退款，
    #    防数据库膨胀 + 保留 LangGraph 退款状态机演示能力）
    try:
        user = register_user(
            db,
            username=username,
            password=password,
            display_name="访客体验者",
            email=None,
            role="visitor",
        )
    except ValueError as e:
        # 极小概率 UUID 冲突，重试一次
        logger.warning(f"Demo 账号创建冲突，重试: {e}")
        username = f"visitor_{uuid.uuid4().hex[:8]}"
        password = secrets.token_urlsafe(16)
        user = register_user(
            db, username=username, password=password,
            display_name="访客体验者", email=None, role="visitor",
        )

    # 3. 发 token + cookie
    token = create_access_token(subject=user.id, extra={"role": user.role})
    _set_token_cookie(response, token)

    # 4. 异步清理（best-effort，失败不影响主流程）
    try:
        cleaned = _cleanup_expired_demo_users(db)
        if cleaned:
            logger.info(f"已清理 {cleaned} 个过期 demo 账号")
    except Exception as e:
        logger.warning(f"清理过期 demo 账号失败（非阻塞）: {e}")

    logger.info(f"Demo 账号创建成功: {username} (id={user.id})")

    return LoginResponse(user=UserOut.model_validate(user))


@router.get("/status")
def public_status():
    """
    公开状态页（无敏感信息）
    用于：HR/面试官快速确认服务存活
    """
    return {
        "status": "ok",
        "env": settings.APP_ENV,
        "version": "0.2.0",
        "demo_login_enabled": settings.ENABLE_DEMO_LOGIN,
    }
