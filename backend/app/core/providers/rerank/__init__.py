"""
core.providers.rerank — Rerank Provider 公开接口

业务模块统一从此处导入：
    from app.core.providers.rerank import get_rerank_provider, RerankProvider
"""
from app.core.providers.rerank.protocols import RerankProvider
from app.core.providers.rerank.qwen_provider import QwenRerankProvider

_provider: RerankProvider | None = None


def get_rerank_provider() -> RerankProvider:
    """获取 Rerank Provider 单例（懒加载）。

    当前唯一实现：QwenRerankProvider（LLM-based batch 评估）。
    """
    global _provider
    if _provider is None:
        _provider = QwenRerankProvider()
    return _provider


__all__ = ["RerankProvider", "QwenRerankProvider", "get_rerank_provider"]