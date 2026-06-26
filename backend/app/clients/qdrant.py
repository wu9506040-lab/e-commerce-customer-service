"""
Qdrant 向量库客户端封装

按 §6 规则：只做连接，不做业务逻辑（不调 embedding、不切片、不生成 ID）
上层（RAG pipeline）调本模块完成向量读写，业务编排在 services/rag/
"""
import logging
from typing import List, Dict, Any, Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

# =============================================================
# 配置
# =============================================================
QDRANT_URL = settings.QDRANT_URL
QDRANT_COLLECTION = settings.QDRANT_COLLECTION
# DashScope text-embedding-v3 维度
VECTOR_SIZE = 1024
DISTANCE = Distance.COSINE

# 单例 client
_client: Optional[QdrantClient] = None


# =============================================================
# 连接管理
# =============================================================
def get_client() -> QdrantClient:
    """获取 Qdrant 客户端（单例）"""
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, timeout=30.0)
        logger.info(f"qdrant client 初始化: {QDRANT_URL}")
    return _client


# =============================================================
# Collection 管理
# =============================================================
def ensure_collection(
    collection_name: Optional[str] = None,
    vector_size: int = VECTOR_SIZE,
) -> bool:
    """
    确保 collection 存在，不存在则创建

    Returns:
        True 表示 collection 已存在或新创建
    """
    name = collection_name or QDRANT_COLLECTION
    client = get_client()

    try:
        collections = client.get_collections().collections
        exists = any(c.name == name for c in collections)

        if exists:
            logger.info(f"qdrant collection '{name}' 已存在")
            return True

        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=DISTANCE),
        )
        logger.info(
            f"qdrant collection '{name}' 创建成功 "
            f"(size={vector_size}, distance={DISTANCE.name})"
        )
        return True
    except Exception:
        logger.exception(f"qdrant collection '{name}' 初始化失败")
        raise


# =============================================================
# 向量读写
# =============================================================
def upsert_points(
    points: List[PointStruct],
    collection_name: Optional[str] = None,
) -> int:
    """
    写入向量点

    Args:
        points: PointStruct 列表（id + vector + payload）
        collection_name: 默认从环境变量读

    Returns:
        写入的点数
    """
    name = collection_name or QDRANT_COLLECTION
    client = get_client()

    operation_info = client.upsert(
        collection_name=name,
        points=points,
        wait=True,
    )
    logger.info(
        f"qdrant upsert: {len(points)} points to '{name}', "
        f"status={operation_info.status}"
    )
    return len(points)


def search(
    query_vector: List[float],
    top_k: int = 5,
    collection_name: Optional[str] = None,
    score_threshold: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    向量搜索

    Args:
        query_vector: 查询向量
        top_k: 返回前 k 个
        collection_name: 默认环境变量
        score_threshold: 相似度阈值（None = 不过滤）

    Returns:
        [{"id": ..., "score": ..., "payload": {...}}, ...]
    """
    name = collection_name or QDRANT_COLLECTION
    client = get_client()

    results = client.search(
        collection_name=name,
        query_vector=query_vector,
        limit=top_k,
        score_threshold=score_threshold,
        with_payload=True,
        with_vectors=False,
    )

    return [
        {
            "id": str(r.id),
            "score": float(r.score),
            "payload": r.payload or {},
        }
        for r in results
    ]


def delete_points(
    point_ids: List,
    collection_name: Optional[str] = None,
) -> int:
    """
    按 ID 删向量点

    Args:
        point_ids: 点 ID 列表（int 或 str）
    """
    name = collection_name or QDRANT_COLLECTION
    client = get_client()

    client.delete(collection_name=name, points_selector=point_ids, wait=True)
    logger.info(f"qdrant delete: {len(point_ids)} points from '{name}'")
    return len(point_ids)


# =============================================================
# 信息查询
# =============================================================
def get_collection_info(collection_name: Optional[str] = None) -> Dict[str, Any]:
    """
    获取 collection 信息

    Returns:
        {"name", "vectors_count", "points_count", "status", "vector_size"}
    """
    name = collection_name or QDRANT_COLLECTION
    client = get_client()

    info = client.get_collection(collection_name=name)
    return {
        "name": name,
        "vectors_count": info.vectors_count,
        "points_count": info.points_count,
        "status": info.status.name if info.status else None,
        "vector_size": info.config.params.vectors.size if info.config and info.config.params.vectors else None,
    }
