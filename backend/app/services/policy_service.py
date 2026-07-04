"""
政策服务 - 政策 RAG（doc_type=policy 过滤）

按 PROJECT_DESIGN.md §3：policy_query 走 RAG，过滤 doc_type='policy' 排除商品/FAQ。

M8：埋点 retrieve_hits + hit@K（policy 检索是 synthesizer 主路径，必须统计）

P1-检索 A：两阶段检索（粗排 → rerank 精排）
- USE_RERANK=true 时：Qdrant top-15 → LLM rerank → top-3
- USE_RERANK=false 时：保持原 Qdrant 直接 top-k（向后兼容）
- rerank 失败时降级到原始排序（rerank.py 内部处理）

P1-检索 B：混合检索（dense vector + BM25 + RRF）
- USE_HYBRID_BM25=true 时：
    1. Qdrant dense vector top-K
    2. BM25 稀疏检索 top-K（从 Qdrant corpus 内存索引）
    3. RRF 融合（Cormack 2009, k=60）→ top-K 融合候选
    4. 可选：rerank 精排 → top-3
- USE_HYBRID_BM25=false 时：仅 dense vector（兼容旧路径）
- BM25 索引构建失败时降级到仅 vector 路径（业务不崩）
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
            - rrf_score: 仅混合检索时存在
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

            # P1-检索 B：混合检索（vector + BM25 + RRF）
            if settings.USE_HYBRID_BM25:
                try:
                    from app.services.bm25_index import bm25_search
                    from app.services.rrf import rrf_fuse

                    bm25_hits = bm25_search(query, top_k=settings.BM25_TOP_K)
                    if bm25_hits:
                        # RRF 融合两路
                        fused = rrf_fuse(
                            [hits, bm25_hits],
                            k=settings.RRF_K,
                        )
                        # 截断到 coarse_top_k（保留送 rerank 的候选数）
                        hits = fused[:coarse_top_k]
                        logger.info(
                            f"hybrid search: vector={len(hits)}/{coarse_top_k} "
                            f"bm25={len(bm25_hits)}/{settings.BM25_TOP_K} "
                            f"→ fused={len(fused)} → top{coarse_top_k}"
                        )
                except Exception as e:
                    # BM25 索引构建失败或 RRF 异常 → 降级到纯 vector
                    logger.warning(f"hybrid 检索失败，降级到纯 vector: {e}")

            # P1-检索 A：rerank 精排
            if settings.USE_RERANK and len(hits) > top_k:
                try:
                    from app.services.rerank import rerank
                    hits = rerank(query, hits, top_n=top_k)
                    logger.info(
                        f"policy rerank: candidates={len(hits)}/{coarse_top_k} "
                        f"→ fine={len(hits)}"
                    )
                except Exception as e:
                    # rerank 失败 → 降级到原始排序 + 截断 top_k（不影响业务）
                    logger.warning(f"policy rerank 失败，降级到粗排: {e}")
                    hits = hits[:top_k]

            metrics.record_hit_at_k(1 if hits else 0)
            return [
                {
                    "text": h.get("payload", {}).get("text", "") or h.get("text", ""),
                    "source": h.get("payload", {}).get("source", "") or h.get("source", ""),
                    "score": h.get("score", 0.0),
                    "rerank_score": h.get("rerank_score"),  # rerank 才有；降级时 None
                    "rrf_score": h.get("rrf_score"),  # 混合检索才有
                }
                for h in hits
            ]
        except Exception as e:
            logger.warning(f"PolicyService.search 失败: {e}")
            metrics.record_hit_at_k(0)  # M8：检索失败算未命中
            return []