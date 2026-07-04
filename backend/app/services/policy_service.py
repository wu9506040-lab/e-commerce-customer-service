"""
政策服务 - 政策 RAG（doc_type=policy 过滤）

按 PROJECT_DESIGN.md §3：policy_query 走 RAG，过滤 doc_type='policy' 排除商品/FAQ。

M8：埋点 retrieve_hits + hit@K（policy 检索是 synthesizer 主路径，必须统计）
"""
import logging
from typing import Optional

from app.clients.qdrant import QDRANT_COLLECTION, search as qdrant_search
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

# Qdrant collection 名（与现有 KB 对齐）
# V2.5 注：M2 阶段 KB 67 条全部是政策（source 命名 policy_* / warranty_*），
# 没有 doc_type 字段，不做后过滤。等 V2.6 引入商品/政策混合 KB 时再加 doc_type 过滤。
# M13 修复：原硬编码 "knowledge_base" 与 ingest 的 "faq_v1" 不一致 → 政策检索 0 命中
COLLECTION_NAME = QDRANT_COLLECTION


class PolicyService:
    """政策 RAG 服务"""

    @staticmethod
    def search_policy(query: str, top_k: int = 3) -> list[dict]:
        """
        检索政策 KB（退货/保修/物流/促销等）

        Args:
            query: 用户问题
            top_k: 返回条数

        Returns:
            [{"text": str, "source": str, "score": float, "doc_type": str}, ...]
        """
        from app.core.embedding import embed_text

        try:
            query_vec = embed_text(query)
        except Exception as e:
            logger.warning(f"PolicyService.embed_text 失败: {e}")
            return []

        try:
            # V2.5: KB 全部是政策，直接 top_k 检索不做 doc_type 过滤
            hits = qdrant_search(
                query_vector=query_vec,
                top_k=top_k,
                score_threshold=0.4,
                collection_name=COLLECTION_NAME,
            )
            # M8：埋点
            metrics.record_retrieve_hits(len(hits))
            metrics.record_hit_at_k(1 if hits else 0)
            return [
                {
                    "text": h.get("payload", {}).get("text", ""),
                    "source": h.get("payload", {}).get("source", ""),
                    "score": h.get("score", 0.0),
                }
                for h in hits
            ]
        except Exception as e:
            logger.warning(f"PolicyService.search 失败: {e}")
            metrics.record_hit_at_k(0)  # M8：检索失败算未命中
            return []