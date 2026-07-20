"""KnowledgeSource Protocol（CLAUDE.md §9.3.3 落地）

任意知识库接入抽象。当前默认实现：QdrantImpl + 混合检索（BM25 + 向量）。
"""
from typing import Protocol, List, Optional
from app.schemas.knowledge import SearchResult, Document


class KnowledgeSource(Protocol):
    """知识库协议"""

    source_type: str  # "qdrant" | "elasticsearch" | "filesystem" | ...

    async def search(
        self, query: str, top_k: int = 5,
        filters: Optional[dict] = None,
    ) -> List[SearchResult]:
        """关键词/语义检索 → 召回 top_k"""
        ...

    async def get_document(self, doc_id: str) -> Optional[Document]:
        """按 doc_id 取完整文档（用于反幻觉审计）"""
        ...

    async def upsert(self, document: Document) -> str:
        """新增/更新文档 → 返 doc_id"""
        ...

    async def delete(self, doc_id: str) -> bool:
        """删除文档 → 返是否成功"""
        ...


class KnowledgeSourceFactory(Protocol):
    def get(self, source_type: str = "qdrant") -> KnowledgeSource: ...


# === 异常类 ===
class KnowledgeError(Exception):
    """知识库通用异常基类"""


class DocumentNotFoundError(KnowledgeError):
    """文档不存在"""