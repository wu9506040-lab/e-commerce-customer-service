"""
千问 (DashScope OpenAI 兼容) 客户端封装

使用 OpenAI 兼容协议调用 DashScope：
https://dashscope.aliyuncs.com/compatible-mode/v1

P1 修复：增加 429 限流自动重试 + 指数退避
"""
import logging
import time
from typing import List, Dict, Optional, Generator

from openai import OpenAI
from openai import RateLimitError

from app.core.config import settings

logger = logging.getLogger(__name__)

# 配置（从 settings 读，已在 core/config.py 集中）
QWEN_API_KEY = settings.QWEN_API_KEY
DASHSCOPE_BASE_URL = settings.DASHSCOPE_BASE_URL
QWEN_MODEL = settings.QWEN_MODEL

# 单例 client（避免每次请求都 new）
_client: Optional[OpenAI] = None

# §9 P1 重试配置：429 限流时指数退避，3 次重试（1s / 2s / 4s）
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # 秒


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


def _is_rate_limit(e: Exception) -> bool:
    """判断是否限流异常（含 429 状态码或 RateLimitError）"""
    if isinstance(e, RateLimitError):
        return True
    msg = str(e)
    return "429" in msg or "rate limit" in msg.lower() or "限流" in msg


def chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> Dict:
    """
    调用千问 chat 端点（带 429 限流自动重试）

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}]
        model: 模型名（默认从环境变量 QWEN_MODEL 读）
        temperature: 温度 0-2
        max_tokens: 最大 token 数（None = 模型默认）

    Returns:
        {"reply": str, "model": str, "usage": dict}

    Raises:
        RateLimitError: 超过 _MAX_RETRIES 次重试仍 429
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

    last_error: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(**kwargs)
            reply = response.choices[0].message.content
            usage = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "total_tokens": response.usage.total_tokens if response.usage else 0,
            }
            logger.info(f"qwen chat done: total_tokens={usage['total_tokens']} attempt={attempt + 1}")
            return {
                "reply": reply,
                "model": used_model,
                "usage": usage,
            }
        except Exception as e:
            if _is_rate_limit(e) and attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE * (2 ** attempt)  # 1s, 2s, 4s
                logger.warning(
                    f"qwen chat 429 retry: attempt={attempt + 1}/{_MAX_RETRIES + 1}, "
                    f"waiting {wait}s, err={str(e)[:100]}"
                )
                time.sleep(wait)
                last_error = e
                continue
            raise

    # 走到这里说明所有重试都失败了
    raise last_error if last_error else RuntimeError("qwen chat failed without exception")


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
    流式调用千问 chat 端点（带 429 连接重试）

    注意：流式中途发生 429 无法直接 retry（已 yield 的 chunk 不可回退），
    所以仅对 client.create() 阶段的 429 做指数退避重试。

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}]
        model: 模型名（默认从环境变量 QWEN_MODEL 读）
        temperature: 温度 0-2
        max_tokens: 单次输出最大 token 数（None = 模型默认；P1 限流时设 512）

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
    stream = None
    last_error: Optional[Exception] = None
    create_kwargs = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if max_tokens is not None:
        create_kwargs["max_tokens"] = max_tokens
    for attempt in range(_MAX_RETRIES + 1):
        try:
            stream = client.chat.completions.create(**create_kwargs)
            break
        except Exception as e:
            if _is_rate_limit(e) and attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE * (2 ** attempt)  # 1s, 2s, 4s
                logger.warning(
                    f"qwen stream_chat 429 retry: attempt={attempt + 1}/{_MAX_RETRIES + 1}, "
                    f"waiting {wait}s, err={str(e)[:100]}"
                )
                time.sleep(wait)
                last_error = e
                continue
            raise

    if stream is None:
        raise last_error if last_error else RuntimeError("qwen stream_chat failed to connect")

    chunk_count = 0
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            chunk_count += 1
            yield chunk.choices[0].delta.content

    logger.info(f"qwen stream_chat done: chunks={chunk_count}")
