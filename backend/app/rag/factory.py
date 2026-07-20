"""KnowledgeSourceFactory — FastAPI Depends 注入

单例缓存 + 按 settings.KNOWLEDGE_SOURCE_TYPE 选择实现。
"""
from functools import lru_cache

from app.core.config import settings
from app.rag.protocols import KnowledgeSource
from app.rag.qdrant_impl import QdrantKnowledgeSource


@lru_cache(maxsize=1)
def get_knowledge_source() -> KnowledgeSource:
    """获取全局 KnowledgeSource（单例）。

    当前实现固定 QdrantKnowledgeSource；后续按 settings.KNOWLEDGE_SOURCE_TYPE
    分支（YAGNI：只有真的接 ES / FS 时再扩）。
    """
    source_type = (settings.KNOWLEDGE_SOURCE_TYPE or "qdrant").lower()
    if source_type == "qdrant":
        return QdrantKnowledgeSource()
    # MVP 阶段：未识别 type 仍返回 Qdrant + 警告（避免生产环境因拼写错误崩溃）
    import logging
    logging.getLogger(__name__).warning(
        f"未知 KNOWLEDGE_SOURCE_TYPE={source_type}，回退 Qdrant 默认实现"
    )
    return QdrantKnowledgeSource()


def reset_knowledge_source() -> None:
    """测试用：清 lru_cache 单例（fail-fast 测试隔离）"""
    get_knowledge_source.cache_clear()