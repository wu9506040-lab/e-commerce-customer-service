"""
千问 (DashScope OpenAI 兼容) 客户端封装

使用 OpenAI 兼容协议调用 DashScope：
https://dashscope.aliyuncs.com/compatible-mode/v1

D 改进：retry + 指数退避 + 抖动 + 断路器
- 扩展可重试错误：429 / 5xx / Timeout / ConnectionError
- 不可重试：400 / 401 / 403（业务错，重试无意义）
- 50% 抖动避免惊群
- 断路器：N 次连续失败 → OPEN → 60s 后 HALF_OPEN 探活
- retry 包 breaker（每次 API call 算 1 个失败，不算 4 个）

面试亮点：
- "QPS 抖动怎么扛？" → "retry+backoff+jitter 让瞬时抖动自愈，breaker 防雪崩"
- "为什么不直接 try/except？" → "断路器防止线程池被慢调用占满"
- "为什么加 jitter？" → "防止多实例同时重试导致惊群（thundering herd）"
"""
import logging
import random
import time
from typing import List, Dict, Optional, Generator

from openai import OpenAI
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

from app.core.config import settings
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

# 配置（从 settings 读，已在 core/config.py 集中）
QWEN_API_KEY = settings.QWEN_API_KEY
DASHSCOPE_BASE_URL = settings.DASHSCOPE_BASE_URL
QWEN_MODEL = settings.QWEN_MODEL

# 单例 client（避免每次请求都 new）
_client: Optional[OpenAI] = None

# 断路器：跨 chat/stream_chat 共享同一个实例（DashScope 是同一个后端）
# 失败计数：连续 N 次失败 → OPEN；恢复 60s 后 HALF_OPEN 探活
_qwen_breaker = CircuitBreaker(
    name="qwen",
    failure_threshold=settings.LLM_CIRCUIT_FAILURE_THRESHOLD,
    recovery_timeout=settings.LLM_CIRCUIT_RECOVERY_TIMEOUT,
    expected_exceptions=(
        RateLimitError,
        InternalServerError,
        APIConnectionError,
        APITimeoutError,
        Exception,  # 网络/未知错误也计入（业务错误 400/401/403 已过滤）
    ),
)


def get_client() -> OpenAI:
    """获取 OpenAI 兼容客户端（单例）"""
    global _client
    if _client is None:
        if not QWEN_API_KEY or QWEN_API_KEY.startswith("sk-put-your-real"):
            raise ValueError(
                "QWEN_API_KEY 未配置或为占位符。"
                "请在 .env.dev 设置真实的 API Key："
                "https://dashscope.console.aliyun.com/apiKey"
            )
        _client = OpenAI(
            api_key=QWEN_API_KEY,
            base_url=DASHSCOPE_BASE_URL,
            timeout=30.0,
        )
    return _client


# =============================================================
# 可重试错误分类（白名单 + 黑名单）
# =============================================================
# 不可重试（业务错，重试无意义）：400 / 401 / 403 / 404
_NON_RETRYABLE_EXCEPTIONS = (
    BadRequestError,        # 400
    AuthenticationError,    # 401
    PermissionDeniedError,  # 403
    NotFoundError,          # 404
)


def _is_retryable(e: Exception) -> bool:
    """判断异常是否应该触发 retry

    策略：
    - 业务错（400/401/403/404）→ 不重试，立即抛
    - 限流/服务端/网络错 → 重试
    - 未知异常 → 默认重试（保守策略，宁可多 retry 也不丢用户问题）
    """
    if isinstance(e, _NON_RETRYABLE_EXCEPTIONS):
        return False
    # openai SDK 的异常体系
    if isinstance(e, (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError)):
        return True
    # 兜底：字符串匹配（兼容老版本 SDK / 第三方包装）
    msg = str(e).lower()
    retryable_signals = ("429", "rate limit", "限流", "500", "502", "503", "504",
                         "timeout", "timed out", "connection", "reset", "unreachable")
    if any(s in msg for s in retryable_signals):
        return True
    # 未知异常默认重试（保守）
    return True


def _calc_backoff(attempt: int, base_delay: float) -> float:
    """计算第 N 次重试的等待时间：base * 2^attempt + 0-50% 抖动

    例：base=1.0
        attempt=0 → 1.0s + 0~0.5s 抖动
        attempt=1 → 2.0s + 0~1.0s 抖动
        attempt=2 → 4.0s + 0~2.0s 抖动
    """
    delay = base_delay * (2 ** attempt)
    jitter = random.uniform(0, delay * 0.5)
    return delay + jitter


# =============================================================
# chat 同步调用
# =============================================================
def chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> Dict:
    """
    调用千问 chat 端点（带 retry + 指数退避 + 抖动 + 断路器）

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}]
        model: 模型名（默认从环境变量 QWEN_MODEL 读）
        temperature: 温度 0-2
        max_tokens: 最大 token 数（None = 模型默认）

    Returns:
        {"reply": str, "model": str, "usage": dict}

    Raises:
        BadRequestError / AuthenticationError / PermissionDeniedError: 业务错（不重试）
        CircuitOpenError: 断路器开路（上游应降级到兜底）
        Exception: 重试耗尽后透传最后一次异常
    """
    client = get_client()
    used_model = model or QWEN_MODEL

    logger.info(f"qwen chat: model={used_model}, messages={len(messages)}")

    kwargs = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    max_retries = settings.LLM_MAX_RETRIES
    base_delay = settings.LLM_RETRY_BASE_DELAY

    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            # 断路器包住单次 API 调用（避免重试 4 次算 4 个失败）
            response = _qwen_breaker.call(client.chat.completions.create, **kwargs)
            reply = response.choices[0].message.content
            usage = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            }
            logger.info(
                f"qwen chat done: total_tokens={usage['total_tokens']} "
                f"attempt={attempt + 1}/{max_retries + 1}"
            )
            return {
                "reply": reply,
                "model": used_model,
                "usage": usage,
            }
        except CircuitOpenError:
            # 断路器开路：不重试，立即抛给上游降级
            logger.warning(
                f"qwen chat 短路（断路器 OPEN）: attempt={attempt + 1}, "
                f"upstream should fallback"
            )
            raise
        except _NON_RETRYABLE_EXCEPTIONS:
            # 业务错：不重试，立即抛
            raise
        except Exception as e:
            last_error = e
            if attempt >= max_retries or not _is_retryable(e):
                logger.error(
                    f"qwen chat 失败（不重试）: attempt={attempt + 1}/{max_retries + 1}, "
                    f"err={type(e).__name__}: {str(e)[:100]}"
                )
                raise
            # 可重试错误：退避后重试
            wait = _calc_backoff(attempt, base_delay)
            logger.warning(
                f"qwen chat retry: attempt={attempt + 1}/{max_retries + 1}, "
                f"waiting {wait:.2f}s, err={type(e).__name__}: {str(e)[:100]}"
            )
            time.sleep(wait)

    # 所有重试都失败
    assert last_error is not None
    raise last_error


# =============================================================
# 流式调用（§14 - 配合 /chat 流式输出）
# =============================================================
def stream_chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> Generator[str, None, None]:
    """
    流式调用千问 chat 端点（带 retry + 断路器）

    注意：流式中途发生异常无法直接 retry（已 yield 的 chunk 不可回退），
    所以仅对 client.create() 阶段做 retry + 断路器。

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}]
        model: 模型名（默认从环境变量 QWEN_MODEL 读）
        temperature: 温度 0-2
        max_tokens: 单次输出最大 token 数

    Yields:
        文本片段（可能为空字符串，已过滤）
    """
    client = get_client()
    used_model = model or QWEN_MODEL

    logger.info(
        f"qwen stream_chat: model={used_model}, messages={len(messages)}, "
        f"max_tokens={max_tokens}"
    )

    # 仅 retry 连接阶段，不 retry 已开始的流
    create_kwargs = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if max_tokens is not None:
        create_kwargs["max_tokens"] = max_tokens

    max_retries = settings.LLM_MAX_RETRIES
    base_delay = settings.LLM_RETRY_BASE_DELAY

    stream = None
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            stream = _qwen_breaker.call(client.chat.completions.create, **create_kwargs)
            break
        except CircuitOpenError:
            logger.warning(
                f"qwen stream_chat 短路（断路器 OPEN）: attempt={attempt + 1}"
            )
            raise
        except _NON_RETRYABLE_EXCEPTIONS:
            raise
        except Exception as e:
            last_error = e
            if attempt >= max_retries or not _is_retryable(e):
                logger.error(
                    f"qwen stream_chat 失败（不重试）: attempt={attempt + 1}/{max_retries + 1}, "
                    f"err={type(e).__name__}: {str(e)[:100]}"
                )
                raise
            wait = _calc_backoff(attempt, base_delay)
            logger.warning(
                f"qwen stream_chat retry: attempt={attempt + 1}/{max_retries + 1}, "
                f"waiting {wait:.2f}s, err={type(e).__name__}: {str(e)[:100]}"
            )
            time.sleep(wait)

    if stream is None:
        raise last_error if last_error else RuntimeError("qwen stream_chat failed to connect")

    chunk_count = 0
    try:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                chunk_count += 1
                yield chunk.choices[0].delta.content
    except (APIConnectionError, APITimeoutError) as e:
        # 流式中途断连（已 yield 部分 token），仅 log，不重试
        logger.warning(
            f"qwen stream_chat 中途断连（已 yield {chunk_count} chunks）: "
            f"{type(e).__name__}: {str(e)[:100]}"
        )
        # 不抛：让上游收到 partial response 自然结束
        return

    logger.info(f"qwen stream_chat done: chunks={chunk_count}")


# =============================================================
# 健康检查 / 测试用
# =============================================================
def get_breaker_stats() -> Dict:
    """导出断路器状态（健康检查 / 调试用）"""
    return _qwen_breaker.stats()


def reset_breaker() -> None:
    """手动重置断路器（管理后台 / 测试用）"""
    _qwen_breaker.reset()
    logger.info("qwen breaker manually reset")