"""
Admin 相关 Pydantic Schema

接口列表：
    POST   /admin/ingest                入库
    GET    /admin/knowledge/info        collection 统计
    GET    /admin/knowledge/sources     按 source 聚合
    DELETE /admin/knowledge/source/{s}  按 source 删全部
    DELETE /admin/knowledge/points      按 point_id 列表删
"""
from typing import List, Optional

from pydantic import BaseModel, Field


# =============================================================
# Ingest
# =============================================================
class IngestRequest(BaseModel):
    """入库请求"""
    text: str = Field(
        ...,
        min_length=1,
        max_length=100_000,
        description="原文（≤ 100KB）",
        examples=["智能客服使用指南第一章..."],
    )
    source: str = Field(
        default="manual",
        min_length=1,
        max_length=200,
        description="来源标识（用于幂等性）",
    )
    chunk_size: int = Field(
        default=500,
        ge=100,
        le=2000,
        description="切片大小（字符数，100-2000）",
    )
    overlap: int = Field(
        default=50,
        ge=0,
        le=499,
        description="切片重叠（字符数，0-499）",
    )
    title: Optional[str] = Field(
        default=None,
        max_length=500,
        description="文档标题（写入 MySQL knowledge_documents 元数据）",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=5000,
        description="文档描述",
    )


class IngestResponse(BaseModel):
    """入库响应"""
    ingested_chunks: int = Field(..., description="实际入库片数")
    source: str = Field(..., description="来源标识")
    chunk_ids: List[str] = Field(
        default_factory=list,
        description="Qdrant 点 ID 列表（按 chunk_index 顺序）",
    )
    chunk_size: int = Field(..., description="实际使用的切片大小")
    overlap: int = Field(..., description="实际使用的重叠大小")


# =============================================================
# Knowledge 管理
# =============================================================
class KnowledgeInfoResponse(BaseModel):
    """collection 统计"""
    name: str = Field(..., description="collection 名")
    points_count: Optional[int] = Field(None, description="点数")
    vectors_count: Optional[int] = Field(None, description="向量数（可能为 None）")
    status: Optional[str] = Field(None, description="collection 状态")
    vector_size: Optional[int] = Field(None, description="向量维度")


class SourceListItem(BaseModel):
    """单个来源统计"""
    source: str = Field(..., description="来源标识")
    count: int = Field(..., description="该来源的 chunk 数")


class SourceListResponse(BaseModel):
    """来源列表"""
    sources: List[SourceListItem] = Field(default_factory=list)
    total_sources: int = Field(..., description="不同来源数")
    total_chunks: int = Field(..., description="总 chunk 数")


class DeleteByIdsRequest(BaseModel):
    """按 point_id 删除请求"""
    chunk_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Qdrant 点 ID 列表",
    )


class DeleteResponse(BaseModel):
    """删除响应"""
    deleted: int = Field(..., description="实际删除数")
    target: str = Field(..., description="删除目标（source 名 或 'by_ids'）")