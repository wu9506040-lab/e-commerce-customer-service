"""
Embedding 客户端 - DashScope text-embedding-v3（OpenAI 兼容模式）

按 §6 规则：core/ 层只做核心能力（embedding 转换）
- 不调外部 HTTP API 路由
- 不做切片/不写 Qdrant（这些是 rag/ 编排）
"""
import logging
from typing import List, Optional

from openai import OpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

# =============================================================
# 配置
# =============================================================
QWEN_API_KEY = settings.QWEN_API_KEY
DASHSCOPE_BASE_URL = settings.DASHSCOPE_BASE_URL
# DashScope text-embedding-v3（1024 维，与 Qdrant collection 匹配）
EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIM = 1024

# 单例 client（与 app/core/qwen.py 共用底层 SDK）
_client: Optional[OpenAI] = None


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
            timeout=30.0,
        )
        logger.info(f"embedding client 初始化: {DASHSCOPE_BASE_URL}")
    return _client


# =============================================================
# Embedding 转换
# =============================================================
def embed_text(text: str) -> List[float]:
    """
    单文本转 embedding

    Args:
        text: 输入文本（≤ 8K tokens）

    Returns:
        1024 维 float 向量
    """
    if not text or not text.strip():
        raise ValueError("embed_text: text 不能为空")

    client = get_client()
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text,
        encoding_format="float",
    )

    embedding = response.data[0].embedding
    logger.info(
        f"embed_text: dim={len(embedding)}, "
        f"prompt_tokens={response.usage.prompt_tokens if response.usage else 0}"
    )
    return embedding


def embed_texts(texts: List[str]) -> List[List[float]]:
    """
    批量文本转 embedding

    Args:
        texts: 文本列表（每个 ≤ 8K tokens）

    Returns:
        List[List[float]]，每个元素是 1024 维向量
    """
    if not texts:
        return []

    # 过滤空字符串（保留索引对应需要上层处理）
    clean_texts = [t if t and t.strip() else " " for t in texts]

    client = get_client()
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=clean_texts,
        encoding_format="float",
    )

    embeddings = [item.embedding for item in response.data]
    logger.info(
        f"embed_texts: count={len(embeddings)}, "
        f"dim={len(embeddings[0]) if embeddings else 0}, "
        f"prompt_tokens={response.usage.prompt_tokens if response.usage else 0}"
    )
    return embeddings


# =============================================================
# 配置查询
# =============================================================
def get_embedding_dim() -> int:
    """返回 embedding 维度（= Qdrant collection vector_size）"""
    return EMBEDDING_DIM


def get_embedding_model() -> str:
    """返回当前使用的 embedding 模型名"""
    return EMBEDDING_MODEL
