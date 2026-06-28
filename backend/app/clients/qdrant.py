"""
Qdrant 向量库客户端封装

按 §6 规则：只做连接，不做业务逻辑（不调 embedding、不切片、不生成 ID）
上层（RAG pipeline）调本模块完成向量读写，业务编排在 services/rag/

健壮性加固（M7）：
- 断路器：连续 3 次失败 → OPEN，30s 后探活
- 降级策略：search 在 OPEN 状态返回 []（让上层走纯 LLM 兜底）
- 降级策略：upsert 在 OPEN 状态返回 0（让数据进 MySQL 不阻塞）
- 健康检查：health_check() 给 /health 端点用

可观测性（M8）：
- search / upsert outcome 上报 metrics（success / fallback_open / error）
"""
import logging
import time
from typing import List, Dict, Any, Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
)

from app.core.config import settings
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.services.metrics import metrics

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

# 断路器：3 次连续失败开路，30s 后探活
_qdrant_breaker = CircuitBreaker(
    name="qdrant",
    failure_threshold=3,
    recovery_timeout=30.0,
    expected_exceptions=(Exception,),
)


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
    写入向量点（带断路器 + 降级）

    降级：断路器开路时返回 0，不抛异常（让 ingest 流程继续走 MySQL 路径）

    Args:
        points: PointStruct 列表（id + vector + payload）
        collection_name: 默认从环境变量读

    Returns:
        写入的点数（断路器开路时返回 0）
    """
    name = collection_name or QDRANT_COLLECTION

    def _do_upsert() -> int:
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

    try:
        result = _qdrant_breaker.call(_do_upsert)
        metrics.inc_qdrant_search("success")  # M8：upsert 也复用此计数器（M8 简化）
        return result
    except CircuitOpenError as e:
        logger.warning(
            f"qdrant upsert 降级（断路器开路）: {len(points)} points skipped, "
            f"retry after {e.retry_after:.1f}s"
        )
        metrics.inc_qdrant_search("fallback_open")
        return 0
    except Exception:
        metrics.inc_qdrant_search("error")
        # 业务异常透传（断路器已自动计数）
        raise


def search(
    query_vector: List[float],
    top_k: int = 5,
    collection_name: Optional[str] = None,
    score_threshold: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    向量搜索（带断路器 + 降级）

    降级：断路器开路时返回 []，让上层走纯 LLM 兜底

    Args:
        query_vector: 查询向量
        top_k: 返回前 k 个
        collection_name: 默认环境变量
        score_threshold: 相似度阈值（None = 不过滤）

    Returns:
        [{"id": ..., "score": ..., "payload": {...}}, ...]
        断路器开路时返回 []
    """
    name = collection_name or QDRANT_COLLECTION

    def _do_search() -> List[Dict[str, Any]]:
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

    try:
        result = _qdrant_breaker.call(_do_search)
        metrics.inc_qdrant_search("success")
        return result
    except CircuitOpenError as e:
        logger.warning(
            f"qdrant search 降级（断路器开路）: 返回空结果, "
            f"retry after {e.retry_after:.1f}s"
        )
        metrics.inc_qdrant_search("fallback_open")
        return []
    except Exception:
        metrics.inc_qdrant_search("error")
        # 业务异常透传（断路器已自动计数）
        raise


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


# =============================================================
# 健康检查（健壮性加固 M7）
# =============================================================
def health_check(timeout: float = 2.0) -> Dict[str, Any]:
    """
    Qdrant 健康检查（/health 端点用）

    Returns:
        {
            "ok": bool,
            "latency_ms": float,
            "points_count": int | None,
            "circuit": {"state": str, "failure_count": int},
            "error": str | None
        }
    """
    result = {
        "ok": False,
        "latency_ms": 0.0,
        "points_count": None,
        "circuit": _qdrant_breaker.stats(),
        "error": None,
    }
    t0 = time.perf_counter()
    try:
        info = get_collection_info()
        result["ok"] = True
        result["points_count"] = info["points_count"]
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result
