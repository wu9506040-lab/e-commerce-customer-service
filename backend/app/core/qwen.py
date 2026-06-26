"""
千问 (DashScope OpenAI 兼容) 客户端封装

使用 OpenAI 兼容协议调用 DashScope：
https://dashscope.aliyuncs.com/compatible-mode/v1
"""
import logging
from typing import List, Dict, Optional, Generator

from openai import OpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

# 配置（从 settings 读，已在 core/config.py 集中）
QWEN_API_KEY = settings.QWEN_API_KEY
DASHSCOPE_BASE_URL = settings.DASHSCOPE_BASE_URL
QWEN_MODEL = settings.QWEN_MODEL

# 单例 client（避免每次请求都 new）
_client: Optional[OpenAI] = None


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


def chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> Dict:
    """
    调用千问 chat 端点

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}]
        model: 模型名（默认从环境变量 QWEN_MODEL 读）
        temperature: 温度 0-2
        max_tokens: 最大 token 数（None = 模型默认）

    Returns:
        {"reply": str, "model": str, "usage": dict}
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

    response = client.chat.completions.create(**kwargs)

    reply = response.choices[0].message.content
    usage = {
        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
        "total_tokens": response.usage.total_tokens if response.usage else 0,
    }

    logger.info(f"qwen chat done: total_tokens={usage['total_tokens']}")

    return {
        "reply": reply,
        "model": used_model,
        "usage": usage,
    }


# =============================================================
# 流式调用（§14 - 配合 /chat 流式输出）
# =============================================================
def stream_chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
) -> Generator[str, None, None]:
    """
    流式调用千问 chat 端点

    用 OpenAI SDK 的 stream=True，逐 chunk yield 文本片段。
    供 services/rag/pipeline.run_stream() 调用，实现端到端流式输出。

    Args:
        messages: 消息列表 [{"role": "user", "content": "..."}]
        model: 模型名（默认从环境变量 QWEN_MODEL 读）
        temperature: 温度 0-2

    Yields:
        文本片段（可能为空字符串，已过滤）
    """
    client = get_client()
    used_model = model or QWEN_MODEL

    logger.info(f"qwen stream_chat: model={used_model}, messages={len(messages)}")

    stream = client.chat.completions.create(
        model=used_model,
        messages=messages,
        temperature=temperature,
        stream=True,
    )

    chunk_count = 0
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            chunk_count += 1
            yield chunk.choices[0].delta.content

    logger.info(f"qwen stream_chat done: chunks={chunk_count}")
