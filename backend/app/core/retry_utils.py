"""
retry_utils — 通用 retry 工具（Sprint 4 收尾 · 从 qwen.py 提取）

提供 2 个纯函数：
- is_retryable(e): 判断异常是否可重试（429/5xx/timeout/connection → True；4xx 业务错 → False）
- calc_backoff(attempt, base_delay): 指数退避 + 50% 抖动（防止惊群）

历史：从 app/core/qwen.py 提取。qwen.py 改为 import 重导出（保留向后兼容），
     待 Sprint 4 收尾删 qwen.py 时，本模块作为唯一真相源。

CLAUDE.md §7.3 接口就近原则：
- 本模块位于 app/core/，与 circuit_breaker 同层
- 提供 LLM / Embedding / Rerank Provider 共用的 retry 分类与退避计算
- 业务模块（如 synthesizer）通过 import retry_utils 使用
"""
from __future__ import annotations

import random
import re as _re

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)


# =============================================================
# 不可重试异常（业务错 · 重试无意义）
# =============================================================
# 业务错（400/401/403/404）：鉴权失败 / 参数错，重试不会成功
_NON_RETRYABLE_EXCEPTIONS = (
    BadRequestError,        # 400
    AuthenticationError,    # 401
    PermissionDeniedError,  # 403
    NotFoundError,          # 404
)


def is_retryable(e: Exception) -> bool:
    """判断异常是否应该触发 retry。

    策略：
    - 业务错（400/401/403/404）→ 不重试，立即抛
    - 限流/服务端/网络错 → 重试
    - 未知异常 → 默认重试（保守策略，宁可多 retry 也不丢用户问题）

    Args:
        e: 捕获的异常

    Returns:
        True = 可重试；False = 业务错应立即抛
    """
    # 业务错：不重试
    if isinstance(e, _NON_RETRYABLE_EXCEPTIONS):
        return False
    # openai SDK 的可重试异常
    if isinstance(e, (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError)):
        return True
    # 兜底：字符串匹配（兼容老版本 SDK / 第三方包装）
    msg = str(e).lower()
    retryable_signals = (
        "429", "rate limit", "限流", "500", "502", "503", "504",
        "timeout", "timed out", "connection", "reset", "unreachable",
    )
    if any(s in msg for s in retryable_signals):
        return True
    # 未知异常默认重试（保守）
    return True


def calc_backoff(attempt: int, base_delay: float) -> float:
    """计算第 N 次重试的等待时间：base * 2^attempt + 0-50% 抖动。

    抖动目的：防止多实例同时重试导致惊群（thundering herd）。

    例子（base=1.0）：
        attempt=0 → 1.0s + 0~0.5s 抖动
        attempt=1 → 2.0s + 0~1.0s 抖动
        attempt=2 → 4.0s + 0~2.0s 抖动

    Args:
        attempt: 当前重试次数（从 0 开始）
        base_delay: 基础等待时间（秒）

    Returns:
        实际等待时间（秒，含抖动）
    """
    delay = base_delay * (2 ** attempt)
    jitter = random.uniform(0, delay * 0.5)
    return delay + jitter


__all__ = ["is_retryable", "calc_backoff"]