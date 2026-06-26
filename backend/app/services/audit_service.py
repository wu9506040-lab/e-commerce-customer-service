"""
审计日志服务 - 记录关键操作（ingest / delete_knowledge / chat / login 等）

按 §6 规则：services/ 编排层，调 models/operation_log

设计要点：
- 独立 session（避免污染调用方事务）
- 写失败仅 warning，不抛（audit 是 best-effort，不能影响主流程）
- 不存消息正文（消息正文在 messages 表）
"""
import logging
from typing import Optional

from app.clients.mysql_client import with_safe_session
from app.models.operation_log import OperationLog
from app.models.user import User

logger = logging.getLogger(__name__)


def try_log_action(
    user: Optional[User],
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    detail: Optional[dict] = None,
    result: str = "success",
    error_msg: Optional[str] = None,
) -> None:
    """
    尝试记录一条 audit；失败仅 warning，不抛异常

    Args:
        user: 操作用户（None = 匿名 / 未登录）
        action: 动作标识（'ingest' / 'delete_knowledge' / 'chat' / 'login' ...）
        target_type: 对象类型（'knowledge' / 'session' / 'user'）
        target_id: 对象 ID
        ip: 来源 IP（从 Request.client.host 取）
        user_agent: UA（从 Request.headers 取，截断 500 字符）
        detail: 额外参数 dict（自动 JSON 序列化）
        result: 'success' / 'fail'
        error_msg: 失败原因（result=fail 时填写）
    """
    # with_safe_session 内部已吞咽所有异常 + warning
    with with_safe_session(commit=True) as db:
        log = OperationLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ip=ip,
            user_agent=user_agent,
            detail=detail,
            result=result,
            error_msg=error_msg,
        )
        db.add(log)
        logger.debug(
            f"audit: action={action}, target={target_type}/{target_id}, "
            f"user={user.username if user else 'anonymous'}"
        )
