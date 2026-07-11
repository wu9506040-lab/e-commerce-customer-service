"""
core.providers.embedding — Embedding Provider 公开接口

业务模块统一从此处导入：
    from app.core.providers.embedding import get_embedding_provider, EmbeddingProvider, EmbeddingError
"""
from app.core.providers.embedding.protocols import EmbeddingError, EmbeddingProvider
from app.core.providers.embedding.qwen_provider import QwenEmbeddingProvider

_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """获取 Embedding Provider 单例（懒加载）。

    当前唯一实现：QwenEmbeddingProvider。
    """
    global _provider
    if _provider is None:
        _provider = QwenEmbeddingProvider()
    return _provider


__all__ = [
    "EmbeddingError",
    "EmbeddingProvider",
    "QwenEmbeddingProvider",
    "get_embedding_provider",
]