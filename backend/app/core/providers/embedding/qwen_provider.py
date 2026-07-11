"""
QwenEmbeddingProvider — Qwen (DashScope) Embedding 实现

内部委托给 `app.core.embedding` 模块级函数（保留 retry + 降级）。
不改业务逻辑，仅做方法签名适配 Protocol。
"""
from typing import List

from app.core import embedding as _legacy_embedding
from app.core.providers.embedding.protocols import EmbeddingError

# 导出 EmbeddingError 以便旧代码继续使用（向后兼容）
__all__ = ["QwenEmbeddingProvider", "EmbeddingError"]


class QwenEmbeddingProvider:
    """Qwen Embedding Provider 实现（text-embedding-v3，1024 维）。

    复用 `app.core.embedding` 模块级 embed_text / embed_texts（含 429/超时 retry）。
    """

    def embed_text(self, text: str) -> List[float]:
        return _legacy_embedding.embed_text(text)

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return _legacy_embedding.embed_texts(texts)

    def get_dim(self) -> int:
        return _legacy_embedding.get_embedding_dim()

    def get_model(self) -> str:
        return _legacy_embedding.get_embedding_model()