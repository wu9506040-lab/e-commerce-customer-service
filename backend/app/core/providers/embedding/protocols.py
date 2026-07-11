"""
EmbeddingProvider Protocol — 文本向量化抽象

按 CLAUDE.md §9.3.3：业务模块禁止直接调用第三方 Embedding SDK。
"""
from typing import Protocol, List, runtime_checkable


class EmbeddingError(Exception):
    """Embedding 调用失败（重试耗尽 / 鉴权失败等）。

    业务模块可捕获此异常做降级（如返回零向量兜底或调用 mock）。
    """
    pass


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embedding 能力抽象。

    业务模块通过 `get_embedding_provider()` 获取实例。
    当前唯一实现：QwenEmbeddingProvider（基于 DashScope text-embedding-v3，1024 维）。
    """

    def embed_text(self, text: str) -> List[float]:
        """单文本转 embedding（带 429/超时 retry）。

        Args:
            text: 输入文本（≤ 8K tokens）

        Returns:
            1024 维 float 向量（具体维度由实现决定，调 get_dim() 获取）

        Raises:
            ValueError: text 为空
            EmbeddingError: 多次重试后仍失败
        """
        ...

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量文本转 embedding（带 429/超时 retry）。

        Args:
            texts: 文本列表（每个 ≤ 8K tokens）

        Returns:
            List[List[float]]，每个元素是 N 维向量

        Raises:
            EmbeddingError: 多次重试后仍失败
        """
        ...

    def get_dim(self) -> int:
        """返回向量维度（替代原 `EMBEDDING_DIM` 常量）。"""
        ...

    def get_model(self) -> str:
        """返回当前使用的 embedding 模型名（替代原 `EMBEDDING_MODEL` 常量）。"""
        ...