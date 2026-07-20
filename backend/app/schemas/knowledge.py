"""Sprint 17: 知识库 DTO（SearchResult / Document）

按 spec §3.3：KnowledgeSource Protocol 统一出入参。
"""
from typing import Optional
from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    """知识库检索结果（KnowledgeSource.search 出参）"""

    doc_id: str
    content: str
    score: float = Field(..., ge=0.0, le=1.0, description="相似度分数")
    metadata: dict = Field(default_factory=dict)


class Document(BaseModel):
    """知识库完整文档（get_document / upsert 入参）"""

    doc_id: Optional[str] = None
    title: str
    content: str
    category: Optional[str] = None
    metadata: dict = Field(default_factory=dict)