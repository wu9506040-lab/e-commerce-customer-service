"""
Embedding 客户端 - DashScope text-embedding-v3（OpenAI 兼容模式）

本模块是 `app.core.providers.embedding.QwenEmbeddingProvider` 的内部 DashScope 客户端，
保存实际的 HTTP / retry / 降级逻辑。**业务模块禁止直接 import 本模块**，
应通过 Provider 抽象调用：`from app.core.providers.embedding import get_embedding_provider`。

按 §6 规则：core/ 层只做核心能力（embedding 转换）
- 不调外部 HTTP API 路由
- 不做切片/不写 Qdrant（这些是 rag/ 编排）

健壮性加固（M7）：
- 单次请求超时：embeddings.create(..., timeout=10)
- 429 重试：指数退避 1/2/4s
- 降级策略：连续失败 → 抛 EmbeddingError，让上层用 mock 兜底（不静默失败，避免污染检索）

可观测性（M8）：
- 调用 / 重试 / 错误 上报 metrics
"""
import contextvars
import logging
import time
from typing import Dict, List, Optional

from openai import OpenAI
from openai import RateLimitError, APITimeoutError, APIConnectionError

from app.core.config import settings
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

# =============================================================
# V13（2.2）：请求级 embedding 复用
# 审计发现同一 query 在一次 /chat 内最多被真实 embed 4 次
# （guard L2 / 语义缓存读 / 检索 / 缓存写），全是同一文本的重复 API 调用。
# contextvar 在 asyncio Task 内天然隔离、asyncio.to_thread 以 copy_context
# 传递——在 event_generator 入口 begin 一次，同请求各线程共享这张 memo。
# 未 begin（默认 None）时行为与旧版完全一致（批处理/离线脚本不受影响）。
# =============================================================
_embed_memo: contextvars.ContextVar[Optional[Dict[str, List[float]]]] = (
    contextvars.ContextVar("embed_memo", default=None)
)
_EMBED_MEMO_MAX = 8  # 单请求去重文本本就极少，上限只防意外循环调用撑内存


def begin_embed_memo():
    """请求入口开启本上下文 embedding 复用；返回 token（一般无需 reset，随 Task 消亡）"""
    return _embed_memo.set({})


def embed_text(text: str) -> List[float]:
    """单文本 embedding（V13 2.2：请求内同文本命中 memo 直接复用向量）。

    命中不计入 embedding 调用指标（真实 API 调用才是成本）；
    返回副本防调用方原地改写污染 memo。
    """
    memo = _embed_memo.get()
    if memo is None:
        return _embed_text_uncached(text)
    hit = memo.get(text)
    if hit is not None:
        logger.debug(f"embed_text memo hit: text_len={len(text)}")
        return list(hit)
    vec = _embed_text_uncached(text)
    if len(memo) < _EMBED_MEMO_MAX:
        memo[text] = list(vec)
    return vec


# =============================================================
# 配置
# =============================================================
QWEN_API_KEY = settings.QWEN_API_KEY
DASHSCOPE_BASE_URL = settings.DASHSCOPE_BASE_URL
# DashScope text-embedding-v3（1024 维，与 Qdrant collection 匹配）
EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIM = 1024

# 单次请求超时（秒）
EMBEDDING_TIMEOUT = 10.0
# 重试配置：429 限流时指数退避
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # 秒

# 单例 client（与 app/core/qwen.py 共用底层 SDK）
_client: Optional[OpenAI] = None


class EmbeddingError(Exception):
    """Embedding 调用失败（降级用）"""
    pass


# =============================================================
# 连接管理
# =============================================================
def get_client() -> OpenAI:
    """获取 OpenAI 兼容客户端（单例）"""
    global _client
    if _client is None:
        if not QWEN_API_KEY or QWEN_API_KEY.startswith("sk-put-your-real"):
            raise ValueError(
                "QWEN_API_KEY 未配置或为占位符。"
                "请在 .env.dev 设置真实 API Key"
            )
        _client = OpenAI(
            api_key=QWEN_API_KEY,
            base_url=DASHSCOPE_BASE_URL,
            timeout=EMBEDDING_TIMEOUT,
        )
        logger.info(f"embedding client 初始化: {DASHSCOPE_BASE_URL}")
    return _client


# =============================================================
# Embedding 转换
# =============================================================
def _is_retryable(e: Exception) -> bool:
    """判断是否可重试（429 / 超时 / 连接错）"""
    return isinstance(e, (RateLimitError, APITimeoutError, APIConnectionError))


def _embed_text_uncached(text: str) -> List[float]:
    """
    单文本转 embedding（带 429/超时 retry）——真实 API 调用体，
    对外入口是同文件 embed_text()（V13 2.2 请求级 memo 包装）

    Args:
        text: 输入文本（≤ 8K tokens）

    Returns:
        1024 维 float 向量

    Raises:
        ValueError: text 为空
        EmbeddingError: 多次重试后仍失败（让上层用 mock 兜底）
    """
    if not text or not text.strip():
        raise ValueError("embed_text: text 不能为空")

    client = get_client()
    last_error: Optional[Exception] = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=text,
                encoding_format="float",
            )
            embedding = response.data[0].embedding
            logger.info(
                f"embed_text: dim={len(embedding)}, "
                f"prompt_tokens={response.usage.prompt_tokens if response.usage else 0}, "
                f"attempt={attempt + 1}"
            )
            metrics.inc_embedding("success")  # M8
            return embedding
        except (RateLimitError, APITimeoutError, APIConnectionError) as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE * (2 ** attempt)  # 1s / 2s / 4s
                logger.warning(
                    f"embed_text 重试 {attempt + 1}/{_MAX_RETRIES + 1}: "
                    f"{type(e).__name__}, waiting {wait}s"
                )
                metrics.inc_embedding("retry")  # M8
                time.sleep(wait)
                continue
            break
        except Exception as e:
            # 非可重试异常（如 401 鉴权失败）直接抛
            logger.error(f"embed_text 不可重试异常: {type(e).__name__}: {str(e)[:200]}")
            metrics.inc_embedding("error")  # M8
            raise

    logger.error(
        f"embed_text 多次重试后仍失败: {type(last_error).__name__}: "
        f"{str(last_error)[:200]}"
    )
    metrics.inc_embedding("error")  # M8
    raise EmbeddingError(
        f"embedding 失败（{_MAX_RETRIES + 1} 次重试后）: "
        f"{type(last_error).__name__ if last_error else 'unknown'}"
    )


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    批量文本转 embedding（带 429/超时 retry）

    Args:
        texts: 文本列表（每个 ≤ 8K tokens）

    Returns:
        List[List[float]]，每个元素是 1024 维向量

    Raises:
        EmbeddingError: 多次重试后仍失败
    """
    if not texts:
        return []

    # 过滤空字符串（保留索引对应需要上层处理）
    clean_texts = [t if t and t.strip() else " " for t in texts]

    client = get_client()
    last_error: Optional[Exception] = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=clean_texts,
                encoding_format="float",
            )
            embeddings = [item.embedding for item in response.data]
            logger.info(
                f"embed_texts: count={len(embeddings)}, "
                f"dim={len(embeddings[0]) if embeddings else 0}, "
                f"prompt_tokens={response.usage.prompt_tokens if response.usage else 0}, "
                f"attempt={attempt + 1}"
            )
            metrics.inc_embedding("success")  # M8
            return embeddings
        except (RateLimitError, APITimeoutError, APIConnectionError) as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    f"embed_texts 重试 {attempt + 1}/{_MAX_RETRIES + 1}: "
                    f"{type(e).__name__}, waiting {wait}s"
                )
                metrics.inc_embedding("retry")  # M8
                time.sleep(wait)
                continue
            break
        except Exception as e:
            logger.error(f"embed_texts 不可重试异常: {type(e).__name__}: {str(e)[:200]}")
            metrics.inc_embedding("error")  # M8
            raise

    logger.error(
        f"embed_texts 多次重试后仍失败: {type(last_error).__name__}: "
        f"{str(last_error)[:200]}"
    )
    metrics.inc_embedding("error")  # M8
    raise EmbeddingError(
        f"embedding 失败（{_MAX_RETRIES + 1} 次重试后）: "
        f"{type(last_error).__name__ if last_error else 'unknown'}"
    )


# =============================================================
# 降级辅助
# =============================================================
def embed_text_or_mock(text: str) -> List[float]:
    """
    embed_text 的降级版：失败时返回零向量（带警告 log）

    **仅用于非关键路径**（如日志摘要 embedding、监控）。**RAG 检索禁止使用**——
    用零向量检索会污染结果。

    Args:
        text: 输入文本

    Returns:
        1024 维零向量（失败时）或真实 embedding
    """
    try:
        return embed_text(text)
    except EmbeddingError as e:
        logger.warning(
            f"embed_text_or_mock 降级到零向量: {e}. "
            f"警告：RAG 检索禁止用此函数！"
        )
        return [0.0] * EMBEDDING_DIM


# =============================================================
# 配置查询
# =============================================================
def get_embedding_dim() -> int:
    """返回 embedding 维度（= Qdrant collection vector_size）"""
    return EMBEDDING_DIM


def get_embedding_model() -> str:
    """返回当前使用的 embedding 模型名"""
    return EMBEDDING_MODEL
