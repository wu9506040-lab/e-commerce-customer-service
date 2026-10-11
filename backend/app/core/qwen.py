"""
千问 (DashScope OpenAI 兼容) 客户端封装

本模块是 `app.core.providers.llm.QwenLLMProvider` 的内部 DashScope 客户端，
保存实际的 HTTP / retry / 断路器逻辑。**业务模块禁止直接 import 本模块**，
应通过 Provider 抽象调用：`from app.core.providers.llm import get_llm_provider`。

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

from app.core import deadline as _deadline
from app.core.config import settings
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)


class StreamTruncatedError(Exception):
    """V13（1.2 修复，2026-10-10）：流式生成中途断连（已 yield 部分 chunk）。

    修复前：qwen.chat_stream 吞掉 APIConnectionError/APITimeoutError 直接 return，
    上游 stream_llm 以为正常收尾 → **半截答案被当完整答案**落库/落缓存/删 checkpoint，
    且污染缓存会反复供应残答。现在显式抛出，由消费方（stream_dispatcher 等）
    打 truncated 标记做隔离处理。partial_chunks 记录已发出的块数供日志/观测。
    """

    def __init__(self, message: str, partial_chunks: int = 0) -> None:
        super().__init__(message)
        self.partial_chunks = partial_chunks


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
        # V14（3.3）：超时从硬编码进配置；SDK 内建重试清零——项目自有
        # retry+退避+断路器，SDK 默认 max_retries=2 与之复合是"最坏 10 分钟"
        # 的根因放大器（一次 attempt 实为 3 次 HTTP，熔断计数也失真）。
        _client = OpenAI(
            api_key=QWEN_API_KEY,
            base_url=DASHSCOPE_BASE_URL,
            timeout=settings.LLM_CALL_TIMEOUT,
            max_retries=settings.LLM_SDK_MAX_RETRIES,
        )
    return _client


# =============================================================
# 可重试错误分类（白名单 + 黑名单）
# =============================================================
# Sprint 4 收尾：retry 分类与退避计算迁到 core/retry_utils.py
# 本文件保留向后兼容垫片（_is_retryable / _calc_backoff 仍可被旧代码 / 旧测试引用）
from app.core.retry_utils import is_retryable as _is_retryable, calc_backoff as _calc_backoff

# 不可重试（业务错，重试无意义）：400 / 401 / 403 / 404
_NON_RETRYABLE_EXCEPTIONS = (
    BadRequestError,        # 400
    AuthenticationError,    # 401
    PermissionDeniedError,  # 403
    NotFoundError,          # 404
)


# =============================================================
# chat 同步调用
# =============================================================
def chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
    tools: Optional[List[Dict]] = None,
    tool_choice: Optional[str] = None,
    *,
    call_timeout: Optional[float] = None,
    stage: str = "llm.chat",
) -> Dict:
    """
    调用千问 chat 端点（带 retry + 指数退避 + 抖动 + 断路器）

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}]
        model: 模型名（默认从环境变量 QWEN_MODEL 读）
        temperature: 温度 0-2
        max_tokens: 最大 token 数（None = 模型默认）
        tools: OpenAI 风格工具定义列表（None = 不传 tools）。格式:
            [{"type": "function", "function": {"name", "description", "parameters"}}]
        tool_choice: "auto" / "none" / {"type": "function", "function": {"name": "..."}}
            None = 不传（由模型默认行为决定）
        call_timeout: 单次 HTTP 上限（秒）；None 则 settings.LLM_CALL_TIMEOUT。
            实际传给 SDK = min(call_timeout, deadline 剩余预算)。
        stage: 阶段标签（rerank/intent/rewrite/synthesize），错误上报 + deadline metric 用。

    Returns:
        {
            "reply": str,
            "model": str,
            "usage": dict,
            "tool_calls": Optional[List[Dict]]   # 模型决定调用工具时非空
        }

    Raises:
        BadRequestError / AuthenticationError / PermissionDeniedError: 业务错（不重试）
        CircuitOpenError: 断路器开路（上游应降级到兜底）
        DeadlineExceededError: 预算耗尽（快速失败，不重试无意义 requeue）
        Exception: 重试耗尽后透传最后一次异常
    """
    client = get_client()
    used_model = model or QWEN_MODEL

    logger.info(f"qwen chat: model={used_model}, messages={len(messages)} stage={stage}")

    kwargs = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if tools is not None:
        kwargs["tools"] = tools
        logger.info(f"qwen chat: tools={len(tools)}")
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice

    base_cap = float(call_timeout) if call_timeout is not None else settings.LLM_CALL_TIMEOUT
    # V14（3.3）：每 attempt 实时算 timeout（预算收缩），预算耗尽先于重试 sleeping 抛，
    # 比 sleeping 后才知道"白睡"的旧路径省掉分钟级尾部。
    kwargs["timeout"] = _deadline.clamp_timeout(base_cap)
    _deadline.check(stage)

    max_retries = settings.LLM_MAX_RETRIES
    base_delay = settings.LLM_RETRY_BASE_DELAY

    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            # 断路器包住单次 API 调用（避免重试 4 次算 4 个失败）
            response = _qwen_breaker.call(client.chat.completions.create, **kwargs)
            message = response.choices[0].message
            reply = message.content
            usage = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            }
            # C1：提取 tool_calls 为结构化 list[dict]
            tool_calls = None
            if getattr(message, "tool_calls", None):
                tool_calls = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]
            logger.info(
                f"qwen chat done: total_tokens={usage['total_tokens']} "
                f"tool_calls={len(tool_calls) if tool_calls else 0} "
                f"attempt={attempt + 1}/{max_retries + 1}"
            )
            return {
                "reply": reply,
                "model": used_model,
                "usage": usage,
                "tool_calls": tool_calls,
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
            # 预算还能不能撑起"wait + 又一次整次调用"？撑不起就不要再睡 +
            # 不要再发——上层已经没时间等，必须立刻抛让上游降级（不让"白睡 + 白
            # 调"叠加分钟级尾部）
            if not _deadline.can_afford(wait + base_cap + 1.0):
                from app.services.metrics import metrics as _metrics
                _metrics.record_deadline_exceeded(stage)
                logger.warning(
                    f"qwen chat 预算不足跳重试: attempt={attempt + 1}, "
                    f"need~{wait + base_cap + 1:.1f}s, err={type(e).__name__}"
                )
                raise _deadline.DeadlineExceededError(
                    f"budget cannot afford retry (need {wait + base_cap + 1:.1f}s)",
                    stage=stage,
                )
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
    # V14（3.3）：建连前预算检查放在生成器函数体外——Python 生成器函数
    # 体内代码只在 next() 时执行，调用 stream_chat() 本身只返回生成器对象
    # 不跑 check。把守卫推到生成器边界外才能实现"建连前抢先拒绝"的设计意图
    _deadline.check("llm.stream")
    return _stream_chat_inner(messages, model, temperature, max_tokens)


def _stream_chat_inner(
    messages: List[Dict[str, str]],
    model: Optional[str],
    temperature: float,
    max_tokens: Optional[int],
) -> Generator[str, None, None]:
    client = get_client()
    used_model = model or QWEN_MODEL

    logger.info(
        f"qwen stream_chat: model={used_model}, messages={len(messages)}, "
        f"max_tokens={max_tokens}"
    )
    create_kwargs = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "timeout": _deadline.clamp_timeout(settings.LLM_CALL_TIMEOUT),
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
            # 预算还能不能撑起"wait + 又一次建连"？撑不起立即抛，让上层快速失败
            if not _deadline.can_afford(wait + settings.LLM_CALL_TIMEOUT + 1.0):
                from app.services.metrics import metrics as _metrics
                _metrics.record_deadline_exceeded("llm.stream")
                raise _deadline.DeadlineExceededError(
                    f"budget cannot afford stream retry (need {wait + settings.LLM_CALL_TIMEOUT + 1:.1f}s)",
                    stage="llm.stream",
                )
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
            # V14（3.3）：流式逐 chunk 过预算——超线按残答契约处理（与中途断
            # 连同型），避免 stream 把整 90s 预算吃光没机会生成答案
            rem = _deadline.remaining()
            if rem is not None and rem <= 0:
                from app.services.metrics import metrics as _metrics
                _metrics.record_deadline_exceeded("llm.stream.chunk")
                logger.warning(
                    f"qwen stream_chat 预算耗尽（已 yield {chunk_count} chunks）：按残答处理"
                )
                raise StreamTruncatedError(
                    f"budget exhausted after {chunk_count} chunks",
                    partial_chunks=chunk_count,
                )
            if chunk.choices and chunk.choices[0].delta.content:
                chunk_count += 1
                yield chunk.choices[0].delta.content
    except (APIConnectionError, APITimeoutError) as e:
        # V13（1.2）：中途断连不再静默 return——显式抛 StreamTruncatedError，
        # 让消费方知道"这是残答而非完整答案"，做缓存隔离/前端标记。
        # （修复前：吞异常 return → 上游正常收尾 → 半截答案落库+进语义缓存被反复命中）
        logger.warning(
            f"qwen stream_chat 中途断连（已 yield {chunk_count} chunks）: "
            f"{type(e).__name__}: {str(e)[:100]}"
        )
        raise StreamTruncatedError(
            f"stream truncated after {chunk_count} chunks: {type(e).__name__}",
            partial_chunks=chunk_count,
        ) from e

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