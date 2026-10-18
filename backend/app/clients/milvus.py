"""
Milvus 向量库客户端封装（V18.3 Qdrant → Milvus 迁移）

与 app/clients/qdrant.py **同名同签名**——上层调用方通过
app/clients/vector_db.py 门面按 settings.VECTOR_DB 切换，业务代码零感知。

兼容层设计：
- MilvusCompatClient：把 pymilvus.MilvusClient 包装成 Qdrant 风格方法
  （scroll / retrieve / upsert / delete / search）——原直接持有 QdrantClient
  裸调用的模块（knowledge.py scroll 统计、bm25_index.py 全量拉取）零改动
- 模块级函数与 qdrant.py 一一对应（ensure_collection / upsert_points /
  search / delete_points / delete_source_points / get_collection_info /
  health_check / get_client）

字段映射（Qdrant payload → Milvus 显式字段 + dynamic 兜底）：
  id(varchar pk, chunk hash)  vector(FLOAT_VECTOR 1024 COSINE)
  text  channel  source  page  section  title  doc_type  category + dynamic

断路器/降级/metrics 行为与 qdrant.py 对齐。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from types import SimpleNamespace

from pymilvus import MilvusClient, DataType

from app.core.config import settings
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

MILVUS_URI = settings.MILVUS_URI
# collection 名不变（迁移要求 2：Qdrant knowledge_base → Milvus knowledge_base）
QDRANT_COLLECTION = settings.QDRANT_COLLECTION
VECTOR_SIZE = 1024

_client: Optional[MilvusClient] = None

vector_db_breaker = CircuitBreaker(
    name="milvus",
    failure_threshold=3,
    recovery_timeout=30.0,
    expected_exceptions=(Exception,),
)

# Milvus 显式标量字段（其余 payload 键进 dynamic field）
_SCALAR_FIELDS = ("text", "channel", "source", "page", "section", "title",
                  "doc_type", "category")


# =============================================================
# 连接管理
# =============================================================
def get_client() -> MilvusCompatClient:
    """获取 Milvus 兼容客户端（单例）"""
    global _client
    if _client is None:
        raw = MilvusClient(uri=MILVUS_URI, timeout=30.0)
        _client = MilvusCompatClient(raw, QDRANT_COLLECTION, VECTOR_SIZE)
        logger.info(f"milvus client 初始化: {MILVUS_URI}, collection={QDRANT_COLLECTION}")
    return _client


class MilvusCompatClient:
    """Qdrant 风格方法包装（上层裸调用零改动）"""

    def __init__(self, raw: MilvusClient, collection: str, dim: int):
        self._raw = raw
        self._collection = collection
        self._dim = dim

    # ---------- Qdrant scroll 兼容（BM25 全量拉取 / source 统计） ----------
    def scroll(self, collection_name: str, limit: int = 100,
               offset=None, with_payload: bool = True,
               with_vectors: bool = False):
        """Qdrant scroll → Milvus query 分页。返回 (records, next_offset)"""
        start = offset if isinstance(offset, int) else 0
        if with_vectors:
            output_fields = ["vector"]
        else:
            output_fields = ["*"]
        rows = self._raw.query(
            collection_name=collection_name,
            filter="",
            output_fields=output_fields,
            offset=start,
            limit=limit,
        )
        records = [self._row_to_record(r) for r in rows]
        next_offset = start + len(rows) if len(rows) == limit else None
        return records, next_offset

    @staticmethod
    def _row_to_record(row: dict) -> SimpleNamespace:
        """Milvus row dict → Qdrant 风格 record（.id / .payload 属性）"""
        row = dict(row)
        vec = row.pop("vector", None)
        rid = row.pop("id", None)
        return SimpleNamespace(id=rid, payload=row, vector=vec)

    # ---------- Qdrant retrieve 兼容（get_document 反查） ----------
    def retrieve(self, collection_name: str, ids: List[str],
                 with_payload: bool = True):
        filter_expr = " || ".join([f'id == "{i}"' for i in ids]) if len(ids) <= 20 \
            else ""
        rows: List[dict] = []
        if filter_expr:
            rows = self._raw.query(
                collection_name=collection_name,
                filter=filter_expr,
                output_fields=["*"],
            )
        else:  # id 太多退化为逐个查
            for i in ids:
                rows.extend(self._raw.query(
                    collection_name=collection_name,
                    filter=f'id == "{i}"', output_fields=["*"],
                ))
        return [self._row_to_record(r) for r in rows]

    # ---------- 原生客户端透传（ingest 特殊需求） ----------
    def get_collections(self):
        """Qdrant 兼容：返回 [SimpleNamespace(name=...)]"""
        cols = self._raw.list_collections()
        return [SimpleNamespace(name=c) for c in cols]

    def create_collection_if_missing(self, collection_name: str, dim: int) -> None:
        if self._raw.has_collection(collection_name):
            return
        schema = self._raw.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        for f in _SCALAR_FIELDS:
            if f == "page":
                schema.add_field(f, DataType.INT64, nullable=True)
            else:
                schema.add_field(f, DataType.VARCHAR, max_length=8192,
                                 nullable=True)
        index_params = self._raw.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="HNSW",
            metric_type="COSINE", params={"M": 16, "efConstruction": 128},
        )
        self._raw.create_collection(collection_name=collection_name, dimension=dim, schema=schema, index_params=index_params, id_type="varchar", primary_field_name="id", vector_field_name="vector", metric_type="COSINE")
        logger.info(f"milvus collection '{collection_name}' 创建成功 (dim={dim})")


# =============================================================
# Collection 管理（签名同 qdrant.ensure_collection）
# =============================================================
def ensure_collection(collection_name: Optional[str] = None,
                      vector_size: int = VECTOR_SIZE) -> bool:
    name = collection_name or QDRANT_COLLECTION
    client = get_client()
    try:
        client.create_collection_if_missing(name, vector_size)
        return True
    except Exception:
        logger.exception(f"milvus collection '{name}' 初始化失败")
        raise


# =============================================================
# 内部工具：PointStruct-like → Milvus row
# =============================================================
def _points_to_rows(points: List[Any]) -> List[Dict[str, Any]]:
    rows = []
    for pt in points:
        if isinstance(pt, dict):
            pid, vec, payload = pt.get("id"), pt.get("vector"), pt.get("payload", {})
        else:  # qdrant PointStruct（属性访问）
            pid, vec, payload = pt.id, pt.vector, (pt.payload or {})
        row = {"id": str(pid), "vector": list(vec)}
        for k, v in (payload or {}).items():
            row[k] = v if v is not None else ""
        rows.append(row)
    return rows


# =============================================================
# 向量读写（签名同 qdrant.py）
# =============================================================
def upsert_points(points: List[Any], collection_name: Optional[str] = None) -> int:
    name = collection_name or QDRANT_COLLECTION

    def _do_upsert() -> int:
        client = get_client()
        rows = _points_to_rows(points)
        res = client._raw.upsert(collection_name=name, data=rows)
        logger.info(f"milvus upsert: {len(rows)} points to '{name}', {res}")
        return len(rows)

    try:
        result = vector_db_breaker.call(_do_upsert)
        metrics.inc_qdrant_search("success")
        return result
    except CircuitOpenError as e:
        logger.warning(f"milvus upsert 降级（断路器开路）: {len(points)} skipped")
        metrics.inc_qdrant_search("fallback_open")
        return 0
    except Exception:
        metrics.inc_qdrant_search("error")
        raise


def search(query_vector: List[float], top_k: int = 5,
           collection_name: Optional[str] = None,
           score_threshold: Optional[float] = None,
           channel: Optional[str] = None) -> List[Dict[str, Any]]:
    name = collection_name or QDRANT_COLLECTION
    filter_expr = f'channel == "{channel}"' if channel else ""

    def _do_search() -> List[Dict[str, Any]]:
        client = get_client()
        res = client._raw.search(
            collection_name=name,
            data=[list(query_vector)],
            limit=top_k,
            filter=filter_expr or "",
            output_fields=["*"],
            search_params={"metric_type": "COSINE"},
        )
        hits_out = []
        # pymilvus 返回 [ [ {id, distance, entity}, ... ] ]
        for hit in (res[0] if res else []):
            score = float(hit.get("distance", 0.0))
            if score_threshold is not None and score < score_threshold:
                continue
            entity = hit.get("entity", {}) or {}
            entity.pop("vector", None)
            hits_out.append({"id": str(hit.get("id", "")), "score": score,
                             "payload": entity})
        return hits_out

    try:
        result = vector_db_breaker.call(_do_search)
        metrics.inc_qdrant_search("success")
        return result
    except CircuitOpenError as e:
        logger.warning(f"milvus search 降级（断路器开路）: 返回空结果")
        metrics.inc_qdrant_search("fallback_open")
        return []
    except Exception:
        metrics.inc_qdrant_search("error")
        raise


def delete_points(point_ids: List, collection_name: Optional[str] = None) -> int:
    name = collection_name or QDRANT_COLLECTION
    client = get_client()
    expr = " || ".join([f'id == "{p}"' for p in point_ids])
    res = client._raw.delete(collection_name=name, filter=expr)
    logger.info(f"milvus delete: {len(point_ids)} points from '{name}'")
    return len(point_ids)


def delete_source_points(source: str, collection_name: Optional[str] = None) -> int:
    """delete-before-write 原语：按 source 清旧世代（V13 2.1'）"""
    name = collection_name or QDRANT_COLLECTION
    client = get_client()
    try:
        res = client._raw.delete(collection_name=name, filter=f'source == "{source}"')
        deleted = res.get("delete_count", 0) if isinstance(res, dict) else 0
        if deleted:
            logger.info(f"milvus delete_source: source={source} 预清旧点 {deleted} 个")
        return int(deleted or 0)
    except Exception as e:
        logger.warning(f"milvus delete_source 失败（可能无存量）: {e}")
        return 0


# =============================================================
# 信息查询 / 健康检查（签名同 qdrant.py）
# =============================================================
def get_collection_info(collection_name: Optional[str] = None) -> Dict[str, Any]:
    name = collection_name or QDRANT_COLLECTION
    client = get_client()
    stats = client._raw.get_collection_stats(name)
    desc = client._raw.describe_collection(name)
    dim = None
    for f in desc.get("fields", []):
        if f.get("name") == "vector":
            dim = f.get("params", {}).get("dim")
    return {
        "name": name,
        "vectors_count": stats.get("row_count"),
        "points_count": stats.get("row_count"),
        "status": "loaded",
        "vector_size": int(dim) if dim else None,
    }


def health_check(timeout: float = 2.0) -> Dict[str, Any]:
    result = {"ok": False, "latency_ms": 0.0, "points_count": None,
              "circuit": vector_db_breaker.stats(), "error": None}
    t0 = time.perf_counter()
    try:
        info = get_collection_info()
        result["ok"] = True
        result["points_count"] = info["points_count"]
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


__all__ = ["vector_db_breaker", "get_client", "ensure_collection", "upsert_points", "search",
           "delete_points", "delete_source_points", "get_collection_info",
           "health_check", "QDRANT_COLLECTION", "VECTOR_SIZE"]
