"""
请求级上下文（ContextVar 透传）— M8 可观测性基础设施

按 §6 规则：core/ 层核心能力，提供全链路追踪所需的上下文传播机制。

设计要点：
- 使用 contextvars（asyncio 原生支持），不污染线程局部变量
- 每个字段提供 set/get 辅助函数 + Token 模式（支持嵌套重置）
- 中间件在请求开始时 set，请求结束时 reset（避免跨请求污染）
- 日志 filter 自动读取这些字段注入到每条日志记录
"""
from contextvars import ContextVar, Token
from typing import Optional


# =============================================================
# ContextVar 定义（默认值 "-" 表示无上下文，避免 KeyError）
# =============================================================
# 请求级追踪 ID（每请求唯一）
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# 会话 ID（chat 场景，由 API 层 set）
session_id_var: ContextVar[str] = ContextVar("session_id", default="-")

# 用户 ID（auth 后由 Depends set；匿名 = 0）
user_id_var: ContextVar[int] = ContextVar("user_id", default=0)

# 当前请求的意图（chat 场景由 synthesizer.set，metrics 用）
intent_var: ContextVar[str] = ContextVar("intent", default="-")


# =============================================================
# 辅助 API（更易读，避免重复 var.get()）
# =============================================================
def get_request_id() -> str:
    """获取当前请求 ID"""
    return request_id_var.get()


def set_request_id(rid: str) -> Token:
    """设置请求 ID，返回 token 用于 reset"""
    return request_id_var.set(rid)


def reset_request_id(token: Token) -> None:
    """重置请求 ID（请求结束时调用）"""
    request_id_var.reset(token)


def set_session_id(sid: Optional[str]) -> Token:
    """设置会话 ID（None 视为 '-'）"""
    return session_id_var.set(sid or "-")


def reset_session_id(token: Token) -> None:
    session_id_var.reset(token)


def set_user_id(uid: Optional[int]) -> Token:
    """设置用户 ID（None = 0 匿名）"""
    return user_id_var.set(uid or 0)


def reset_user_id(token: Token) -> None:
    user_id_var.reset(token)


def set_intent(intent: str) -> Token:
    """设置当前意图（synthesizer 分类后调用）"""
    return intent_var.set(intent)


def reset_intent(token: Token) -> None:
    intent_var.reset(token)


def get_all() -> dict:
    """获取所有上下文（debug 用 / /metrics 端点用）"""
    return {
        "request_id": request_id_var.get(),
        "session_id": session_id_var.get(),
        "user_id": user_id_var.get(),
        "intent": intent_var.get(),
    }