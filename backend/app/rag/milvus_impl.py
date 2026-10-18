"""MilvusKnowledgeSource（V18.3 迁移）

底层已通过 app/clients/vector_db.py 门面切换到 Milvus，search/hybrid/校验
逻辑与 QdrantKnowledgeSource 完全一致——本类只改 source_type 标识。
"""
from app.rag.qdrant_impl import QdrantKnowledgeSource


class MilvusKnowledgeSource(QdrantKnowledgeSource):
    source_type = "milvus"
