"""
政策服务 - 政策 RAG（doc_type=policy 过滤）

按 PROJECT_DESIGN.md §3：policy_query 走 RAG，过滤 doc_type='policy' 排除商品/FAQ。

M8：埋点 retrieve_hits + hit@K（policy 检索是 synthesizer 主路径，必须统计）

P1-检索：两阶段检索（粗排 → rerank 精排）
- USE_RERANK=true 时：Qdrant top-15 → LLM rerank → top-3
- USE_RERANK=false 时：保持原 Qdrant 直接 top-k（向后兼容）
- rerank 失败时降级到原始排序（rerank.py 内部处理）
"""
import logging
from typing import Optional

from app.clients.qdrant import QDRANT_COLLECTION, search as qdrant_search
from app.core.config import settings
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
            [{"text": str, "source": str, "score": float, "rerank_score": float|None}, ...]
            - rerank_score: None 表示未走 rerank（USE_RERANK=false 或 rerank 降级）
        """
        from app.core.embedding import embed_text

        try:
            query_vec = embed_text(query)
        except Exception as e:
            logger.warning(f"PolicyService.embed_text 失败: {e}")
            return []

        # 粗排 top-k：USE_RERANK 时取 RERANK_CANDIDATE_TOP_K（默认 15），
        # 否则直接取最终 top_k（兼容旧行为）
        coarse_top_k = (
            settings.RERANK_CANDIDATE_TOP_K
            if settings.USE_RERANK
            else top_k
        )

        try:
            # V2.5: KB 全部是政策，直接 top_k 检索不做 doc_type 过滤
            hits = qdrant_search(
                query_vector=query_vec,
                top_k=coarse_top_k,
                score_threshold=0.4,
                collection_name=COLLECTION_NAME,
            )
            # M8：埋点（粗排命中数，便于对比 rerank 前后）
            metrics.record_retrieve_hits(len(hits))

            # P1-检索：rerank 精排
            if settings.USE_RERANK and len(hits) > top_k:
                try:
                    from app.services.rerank import rerank
                    hits = rerank(query, hits, top_n=top_k)
                    logger.info(
                        f"policy rerank: coarse={len(hits)}/{coarse_top_k} "
                        f"→ fine={len(hits)}"
                    )
                except Exception as e:
                    # rerank 失败 → 降级到原始排序 + 截断 top_k（不影响业务）
                    logger.warning(f"policy rerank 失败，降级到粗排: {e}")
                    hits = hits[:top_k]

            metrics.record_hit_at_k(1 if hits else 0)
            return [
                {
                    "text": h.get("payload", {}).get("text", ""),
                    "source": h.get("payload", {}).get("source", ""),
                    "score": h.get("score", 0.0),
                    "rerank_score": h.get("rerank_score"),  # rerank 才有；降级时 None
                }
                for h in hits
            ]
        except Exception as e:
            logger.warning(f"PolicyService.search 失败: {e}")
            metrics.record_hit_at_k(0)  # M8：检索失败算未命中
            return []