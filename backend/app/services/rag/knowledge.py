"""
Knowledge 管理服务 - 知识库元数据查询与删除（Qdrant + MySQL metadata §11）

按 §6 规则：
- services/ 编排层，可调 clients/qdrant 和 clients/mysql
- 不写 HTTP 路由（HTTP 路由在 api/admin.py）
- 不改 embedding / qwen

提供：
    - get_info()              collection 统计
    - list_sources()          按 source 聚合（每来源多少片）
    - delete_by_source(source) 按 source 删全部（Qdrant + MySQL 软删 status=0）
    - delete_by_ids(ids)      按 point_id 删
"""
import logging
from typing import Dict, List

from qdrant_client.models import Filter, FieldCondition, MatchValue
from sqlalchemy import select

from app.clients.mysql_client import with_safe_session
from app.clients.qdrant import (
    delete_points,
    get_client,
    get_collection_info as _qdrant_get_info,
)
from app.models.knowledge_document import KnowledgeDocument

logger = logging.getLogger(__name__)

# 单次 scroll 拉多少点（list_sources 需要遍历，限速保护）
SCROLL_BATCH = 1000
# scroll 内部 limit 上限
SCROLL_LIMIT = 10000


# =============================================================
# 信息查询
# =============================================================
def get_info() -> Dict:
    """
    获取 collection 统计信息

    Returns:
        {
            "name", "points_count", "vectors_count",
            "status", "vector_size"
        }
    """
    info = _qdrant_get_info()
    logger.info(
        f"knowledge.get_info: name={info['name']}, "
        f"points={info.get('points_count')}, "
        f"vector_size={info.get('vector_size')}"
    )
    return info


def list_sources() -> List[Dict]:
    """
    按 source 字段聚合，统计每个来源的 chunk 数

    Returns:
        [
            {"source": "warranty_policy", "count": 3},
            {"source": "test_guide_v1", "count": 3},
            ...
        ]
    按 count 倒序
    """
    from app.clients.qdrant import QDRANT_COLLECTION
    client = get_client()

    all_sources: Dict[str, int] = {}
    next_offset = None
    total_scanned = 0

    # scroll 分批遍历（只取 payload 不取 vector，省流量）
    while True:
        records, next_offset = client.scroll(
            collection_name=QDRANT_COLLECTION,
            limit=SCROLL_BATCH,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        for r in records:
            payload = r.payload or {}
            source = payload.get("source", "(unknown)")
            all_sources[source] = all_sources.get(source, 0) + 1
            total_scanned += 1

        if not next_offset or total_scanned >= SCROLL_LIMIT:
            break

    # 转成 list 并按 count 倒序
    result = [
        {"source": src, "count": cnt}
        for src, cnt in sorted(all_sources.items(), key=lambda x: x[1], reverse=True)
    ]
    logger.info(
        f"knowledge.list_sources: scanned={total_scanned}, "
        f"distinct_sources={len(result)}"
    )
    return result


# =============================================================
# 删除
# =============================================================
def delete_by_source(source: str) -> int:
    """
    按 source 字段删除该来源的全部点

    Args:
        source: 来源标识

    Returns:
        实际删除的点数（qdrant 不直接返回，借助前后 points_count 差值估算）
    """
    if not source or not source.strip():
        raise ValueError("delete_by_source: source 不能为空")

    from app.clients.qdrant import QDRANT_COLLECTION
    client = get_client()

    # 删除前快照
    before = _qdrant_get_info().get("points_count") or 0

    client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=Filter(
            must=[
                FieldCondition(
                    key="source",
                    match=MatchValue(value=source),
                )
            ]
        ),
        wait=True,
    )

    # 删除后快照
    after = _qdrant_get_info().get("points_count") or 0
    deleted = max(0, before - after)

    # §11 write-through: MySQL 软删 status=0（保审计完整性）
    _soft_delete_knowledge_meta(source)

    logger.info(
        f"knowledge.delete_by_source: source={source}, "
        f"qdrant_deleted={deleted} (before={before}, after={after})"
    )
    return deleted


def _soft_delete_knowledge_meta(source: str) -> None:
    """
    MySQL knowledge_documents 软删（status=0）
    失败仅 warning，不抛（Qdrant 已删，MySQL 软删失败不影响主流程）
    """
    with with_safe_session(commit=True) as db:
        doc = db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source == source,
                KnowledgeDocument.deleted == 0,
            )
        ).scalar_one_or_none()
        if doc is None:
            logger.debug(
                f"_soft_delete_knowledge_meta: source={source} 不存在，无需软删"
            )
            return
        doc.status = 0
        logger.info(
            f"_soft_delete_knowledge_meta: source={source} → status=0"
        )


def delete_by_ids(point_ids: List[str]) -> int:
    """
    按 point_id 列表删除

    Args:
        point_ids: Qdrant 点 ID 列表

    Returns:
        尝试删除的点数
    """
    if not point_ids:
        raise ValueError("delete_by_ids: point_ids 不能为空")
    deleted = delete_points(point_ids)
    logger.info(f"knowledge.delete_by_ids: requested={len(point_ids)}, deleted={deleted}")
    return deleted