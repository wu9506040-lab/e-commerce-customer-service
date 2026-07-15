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

Phase 4 A4: Multi-Query 多路检索
- queries: List[str]
- 每条 query 走一遍 search_policy（含 hybrid + rerank）→ 拿到该路的 top-K 候选
- N 路结果用 RRF 融合 → 截断到 top_k
- 任一路异常 → 仅该路降级（其他路继续）

Phase 4 A5: Multi-Query 并行检索（性能优化）
- 默认 MULTI_QUERY_PARALLEL=True + MULTI_QUERY_WORKERS=3
- ThreadPoolExecutor per-request（with 块，离开自动 shutdown）
- N 路并行 ≈ 1 路耗时（加速比 ~2.9x @ 3 路）
- 关掉并行：MULTI_QUERY_PARALLEL=False 走原串行（debug 用）

Phase 4 A8: 融合后 rerank（LLM 成本优化）
- 默认 MULTI_QUERY_FUSE_FIRST_RERANK=True
- 路径变化：N×(coarse→rerank) → N×coarse → RRF → 1×rerank
- 收益：3×rerank LLM 调用 → 1×，token -66% / 延迟 -66% / rerank 视角更全局
- 关闭回退：A8 flag=False → 走 A5 per-query rerank 路径

Phase 4 A8 实现要点：
- 抽取 _coarse_retrieval 公共方法（search_policy / search_policy_coarse 共用，零漂移）
- 新增 search_policy_coarse（粗排不带 rerank，给 fuse-first 模式用）
- 运行指标日志：[multi_query_metrics] mode= queries= rerank_calls= latency_ms=
  便于 grep 对比 A5 vs A8 真实效果（成本 / 延迟 / 召回质量）
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

from app.clients.qdrant import QDRANT_COLLECTION, search as qdrant_search
from app.core.config import settings
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

# Qdrant collection 名（与现有 KB 对齐）
# V2.5 注：M2 阶段 KB 67 条全部是政策（source 命名 policy_* / warranty_*），
# 没有 doc_type 字段，不做后过滤。等 V2.6 引入商品/政策混合 KB 时再加 doc_type 过滤。
# M13 修复：原硬编码 "knowledge_base" 与 ingest 的 "faq_v1" 不一致 → 政策检索 0 命中
COLLECTION_NAME = QDRANT_COLLECTION


def _format_hits(hits: list[dict]) -> list[dict]:
    """统一格式化输出 schema（search_policy / search_policy_coarse 共用）

    字段：text / source / score / rerank_score / rrf_score
    - rerank_score: None 表示未走 rerank
    - rrf_score: 仅混合检索时存在
    """
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


class PolicyService:
    """政策 RAG 服务"""

    # =============================================================
    # Phase 4 A8 抽取：粗排公共方法（search_policy / search_policy_coarse 共用）
    # =============================================================
    @staticmethod
    def _coarse_retrieval(query: str, coarse_top_k: int) -> list[dict]:
        """粗排公共逻辑（embed → qdrant → hybrid → truncate）。

        search_policy 和 search_policy_coarse 都调本方法，确保两条路径
        在粗排阶段的行为完全一致（避免后续逻辑漂移）。

        Args:
            query: 用户问题
            coarse_top_k: 粗排候选数（默认 = RERANK_CANDIDATE_TOP_K = 15）

        Returns:
            粗排 hits（每条可能含 payload / rrf_score / score）
            返回空 list 表示该 query 检索失败（embed / qdrant 异常）
        """
        from app.core.providers.embedding import get_embedding_provider

        # 1. embedding
        try:
            query_vec = get_embedding_provider().embed_text(query)
        except Exception as e:
            logger.warning(f"PolicyService.embed_text 失败: {e}")
            return []

        # 2. qdrant top-K 粗排
        try:
            hits = qdrant_search(
                query_vector=query_vec,
                top_k=coarse_top_k,
                score_threshold=0.4,
                collection_name=COLLECTION_NAME,
            )
            metrics.record_retrieve_hits(len(hits))
        except Exception as e:
            logger.warning(f"PolicyService qdrant 检索失败: {e}")
            return []

        # 3. hybrid: vector + BM25 + RRF（可选）
        if settings.USE_HYBRID_BM25:
            try:
                from app.services.bm25_index import bm25_search
                from app.services.rrf import rrf_fuse

                bm25_hits = bm25_search(query, top_k=settings.BM25_TOP_K)
                if bm25_hits:
                    fused = rrf_fuse(
                        [hits, bm25_hits],
                        k=settings.RRF_K,
                    )
                    hits = fused[:coarse_top_k]
                    logger.debug(
                        f"hybrid: vector={len(hits)}/{coarse_top_k} "
                        f"bm25={len(bm25_hits)}/{settings.BM25_TOP_K} "
                        f"→ fused={len(fused)} → top{coarse_top_k}"
                    )
            except Exception as e:
                logger.warning(f"hybrid 检索失败，降级到纯 vector: {e}")

        return hits

    # =============================================================
    # 单路检索（保持向后兼容，A4/A5/A8 全部基于它）
    # =============================================================
    @staticmethod
    def search_policy(query: str, top_k: int = 3) -> list[dict]:
        """
        检索政策 KB（退货/保修/物流/促销等）

        流程：_coarse_retrieval → 可选 rerank → top_k

        Args:
            query: 用户问题
            top_k: 返回条数

        Returns:
            [{"text": str, "source": str, "score": float,
              "rerank_score": float|None, "rrf_score": float|None}, ...]
        """
        coarse_top_k = (
            settings.RERANK_CANDIDATE_TOP_K
            if settings.USE_RERANK
            else top_k
        )

        hits = PolicyService._coarse_retrieval(query, coarse_top_k)
        if not hits:
            metrics.record_hit_at_k(0)
            return []

        # 可选：rerank 精排
        if settings.USE_RERANK and len(hits) > top_k:
            try:
                from app.core.providers.rerank import get_rerank_provider
                hits = get_rerank_provider().rerank(query, hits, top_n=top_k)
                logger.debug(
                    f"policy rerank: candidates={len(hits)}/{coarse_top_k} → top{top_k}"
                )
            except Exception as e:
                logger.warning(f"policy rerank 失败，降级到粗排: {e}")
                hits = hits[:top_k]

        metrics.record_hit_at_k(1 if hits else 0)
        return _format_hits(hits)

    # =============================================================
    # Phase 4 A8：粗排（不带 rerank，给 fuse-first 模式用）
    # =============================================================
    @staticmethod
    def search_policy_coarse(query: str, top_k: int | None = None) -> list[dict]:
        """粗排（不带 rerank，给 Phase 4 A8 fuse-first 用）。

        与 search_policy 的区别：
        - search_policy = 粗排 + rerank（单路精排）
        - search_policy_coarse = 仅粗排（多路融合后统一 rerank）

        共用 _coarse_retrieval，零逻辑漂移。

        Args:
            query: 用户问题
            top_k: 粗排候选数（默认 settings.RERANK_CANDIDATE_TOP_K = 15）

        Returns:
            schema 与 search_policy 一致；rerank_score=None（未走 rerank）
        """
        coarse_top_k = (
            top_k if top_k is not None else settings.RERANK_CANDIDATE_TOP_K
        )
        hits = PolicyService._coarse_retrieval(query, coarse_top_k)
        if not hits:
            metrics.record_hit_at_k(0)
            return []
        metrics.record_hit_at_k(1 if hits else 0)
        return _format_hits(hits)

    # =============================================================
    # Phase 4 A4 + A5 + A8: Multi-Query 多路检索
    # =============================================================
    @staticmethod
    def search_multi_policy(
        queries: List[str],
        top_k: int = 3,
    ) -> List[dict]:
        """
        多路检索（fuse-first rerank / per-query rerank / 单路短路）

        Phase 4 A5：默认并行（N 路 ~2.9x 加速）
        Phase 4 A8：默认 fuse-first（3×rerank → 1×rerank，LLM token -66%）

        路径决策（按优先级）：
        1. 单路（len=1）：直接 search_policy，不做 RRF 融合
        2. fuse-first（A8 flag=True + 多路）：
             每路 search_policy_coarse → RRF → 截断 15 → 1×rerank → top-k
        3. per-query rerank（A8 flag=False + 多路，A5 路径）：
             每路 search_policy → RRF 融合

        运行指标（每调用一行日志，便于 A5/A8 对比）：
            [multi_query_metrics] mode= queries= rerank_calls= latency_ms= ...

        Args:
            queries: 多路改写后的查询列表（≥ 1）
            top_k: 返回条数

        Returns:
            [{"text", "source", "score", "rerank_score", "rrf_score"}, ...]
            与 search_policy schema 一致（业务侧零改动消费）
            降级：queries 空 / 全部失败 → 返空 list
        """
        from app.services.rrf import rrf_fuse

        # A8 运行指标：起点计时 + rerank 调用计数
        start = time.perf_counter()
        rerank_call_count = 0
        mode = "unknown"

        if not queries:
            return []

        valid_queries: List[tuple] = [
            (idx, q) for idx, q in enumerate(queries) if q and q.strip()
        ]
        if not valid_queries:
            return []

        # =============================================================
        # 路径 1：单路短路（len=1）
        # =============================================================
        if len(valid_queries) == 1:
            mode = "single"
            result = PolicyService.search_policy(valid_queries[0][1], top_k=top_k)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[multi_query_metrics] mode={mode} queries=1 "
                f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f}"
            )
            return result

        # =============================================================
        # 路径 2：Phase 4 A8 fuse-first rerank（默认）
        # =============================================================
        use_fuse_first = settings.MULTI_QUERY_FUSE_FIRST_RERANK and settings.USE_RERANK
        use_parallel = settings.MULTI_QUERY_PARALLEL

        if use_fuse_first:
            mode = "fuse_first"
            coarse_top_k = settings.RERANK_CANDIDATE_TOP_K
            per_query_hits: List[List[dict]] = []

            if use_parallel:
                # 并行粗排（per-request executor，with 块结束自动 shutdown）
                max_workers = max(
                    1, min(settings.MULTI_QUERY_WORKERS, len(valid_queries))
                )
                with ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="multi-policy",
                ) as ex:
                    futures = {
                        ex.submit(
                            PolicyService.search_policy_coarse, q, top_k=coarse_top_k
                        ): (idx, q)
                        for idx, q in valid_queries
                    }
                    results_by_idx: dict = {}
                    for future in as_completed(futures):
                        idx, q = futures[future]
                        try:
                            hits = future.result()
                            for h in hits:
                                h.setdefault("id", h.get("source", ""))
                            results_by_idx[idx] = hits
                        except Exception as e:
                            logger.warning(
                                f"search_multi_policy[A8] 第 {idx} 路失败 "
                                f"(q='{q[:30]}...'): {e}"
                            )
                            metrics.record_hit_at_k(0)
                    per_query_hits = [
                        results_by_idx[i] for i in sorted(results_by_idx.keys())
                    ]
            else:
                # 串行 fuse-first（debug 用）
                for idx, q in valid_queries:
                    try:
                        hits = PolicyService.search_policy_coarse(q, top_k=coarse_top_k)
                        for h in hits:
                            h.setdefault("id", h.get("source", ""))
                        per_query_hits.append(hits)
                    except Exception as e:
                        logger.warning(
                            f"search_multi_policy[A8 serial] 第 {idx} 路失败 "
                            f"(q='{q[:30]}...'): {e}"
                        )
                        metrics.record_hit_at_k(0)

            if not per_query_hits:
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    f"[multi_query_metrics] mode={mode} queries={len(valid_queries)} "
                    f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                    f"result=empty"
                )
                return []

            # RRF 融合全局候选
            try:
                fused = rrf_fuse(per_query_hits, k=settings.RRF_K)
            except Exception as e:
                logger.warning(
                    f"search_multi_policy[A8] RRF 失败，降级到首路前 {top_k}: {e}"
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    f"[multi_query_metrics] mode={mode}_rrf_fail "
                    f"queries={len(valid_queries)} "
                    f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                    f"fallback=first"
                )
                return per_query_hits[0][:top_k]

            # 截断到 MAX_CANDIDATES_PER_CALL 后送 rerank（用 queries[0] 作评估 query）
            rerank_input = fused[:coarse_top_k]
            if len(rerank_input) > top_k:
                try:
                    from app.core.providers.rerank import get_rerank_provider
                    rerank_call_count = 1  # A8 关键：1 次 rerank 调用
                    reranked = get_rerank_provider().rerank(
                        valid_queries[0][1],  # 原始 query（语义最准）
                        rerank_input,
                        top_n=top_k,
                    )
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.info(
                        f"[multi_query_metrics] mode={mode} queries={len(valid_queries)} "
                        f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                        f"fused={len(fused)} rerank_in={len(rerank_input)} → top{top_k}"
                    )
                    return reranked
                except Exception as e:
                    logger.warning(
                        f"search_multi_policy[A8] rerank 失败，降级到 RRF top-{top_k}: {e}"
                    )
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.info(
                        f"[multi_query_metrics] mode={mode}_rerank_fail "
                        f"queries={len(valid_queries)} "
                        f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                        f"fused={len(fused)} fallback=rrf_topk"
                    )
                    return fused[:top_k]
            else:
                # 候选数 ≤ top_k，无需 rerank
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    f"[multi_query_metrics] mode={mode}_no_rerank "
                    f"queries={len(valid_queries)} "
                    f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                    f"fused={len(fused)} → top{top_k}"
                )
                return fused[:top_k]

        # =============================================================
        # 路径 3：A5 per-query rerank（fuse_first=False 时回退）
        # =============================================================
        mode = "per_query"
        per_query_hits: List[List[dict]] = []
        total_hits = 0

        if use_parallel:
            max_workers = max(
                1, min(settings.MULTI_QUERY_WORKERS, len(valid_queries))
            )
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="multi-policy",
            ) as ex:
                futures = {
                    ex.submit(PolicyService.search_policy, q, top_k=top_k): (idx, q)
                    for idx, q in valid_queries
                }
                results_by_idx: dict = {}
                for future in as_completed(futures):
                    idx, q = futures[future]
                    try:
                        hits = future.result()
                        for h in hits:
                            h.setdefault("id", h.get("source", ""))
                        results_by_idx[idx] = hits
                        total_hits += len(hits)
                        # 估算 rerank 调用次数（search_policy 内部可能调 rerank）
                        if settings.USE_RERANK:
                            rerank_call_count += 1
                    except Exception as e:
                        logger.warning(
                            f"search_multi_policy[A5] 第 {idx} 路失败 "
                            f"(q='{q[:30]}...'): {e}"
                        )
                        metrics.record_hit_at_k(0)
                per_query_hits = [
                    results_by_idx[i] for i in sorted(results_by_idx.keys())
                ]
        else:
            for idx, q in valid_queries:
                try:
                    hits = PolicyService.search_policy(q, top_k=top_k)
                    for h in hits:
                        h.setdefault("id", h.get("source", ""))
                    per_query_hits.append(hits)
                    total_hits += len(hits)
                    if settings.USE_RERANK:
                        rerank_call_count += 1
                except Exception as e:
                    logger.warning(
                        f"search_multi_policy[A5 serial] 第 {idx} 路失败 "
                        f"(q='{q[:30]}...'): {e}"
                    )
                    metrics.record_hit_at_k(0)

        if not per_query_hits:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[multi_query_metrics] mode={mode} queries={len(valid_queries)} "
                f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                f"result=empty"
            )
            return []

        try:
            fused = rrf_fuse(per_query_hits, k=settings.RRF_K)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[multi_query_metrics] mode={mode} queries={len(valid_queries)} "
                f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                f"hits_total={total_hits} fused={len(fused)} → top{top_k}"
            )
            return fused[:top_k]
        except Exception as e:
            logger.warning(f"search_multi_policy[A5] RRF 失败，降级到首路前 {top_k}: {e}")
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[multi_query_metrics] mode={mode}_rrf_fail queries={len(valid_queries)} "
                f"rerank_calls={rerank_call_count} latency_ms={elapsed_ms:.1f} "
                f"fallback=first"
            )
            return per_query_hits[0][:top_k]