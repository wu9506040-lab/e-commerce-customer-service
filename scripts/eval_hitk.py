"""
eval_hitk.py - 评估 RAG 检索质量：hit@1 / hit@3 / hit@5 / hit@10

加载 data/eval_set_v1.json（query → relevant_doc_id），
对每条 query：
    1. embedding 转 1024 维向量
    2. Qdrant top-10 检索
    3. 判断 relevant_doc_id 是否在前 K 个结果里

输出：
    - 汇总表（hit@1/3/5/10 + 检索时延）
    - 失败案例（前 10 个 miss，方便分析）
    - JSON 详细结果

设计说明：
- hit@K：检索结果前 K 个中是否包含至少一个"相关文档"
- 单一相关文档（每条 query 只有一个 relevant_doc_id）→ binary relevance
- max_k=10：超过 10 的检索在 RAG 场景意义不大（context window 限制）
- 模拟"真实流量"：每次 query 单独 embed + search，不批量（避免 batch 优化掩盖问题）

用法：
    PYTHONPATH=backend python scripts/eval_hitk.py
    # 或指定不同的 eval 集
    PYTHONPATH=backend python scripts/eval_hitk.py --input data/eval_set_v2.json
"""
import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path
from typing import List, Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

try:
    from dotenv import load_dotenv  # type: ignore
    for env_file in [
        BACKEND_DIR / ".env",
        PROJECT_ROOT / "deploy" / ".env.dev",
        PROJECT_ROOT / ".env",
    ]:
        if env_file.exists():
            load_dotenv(env_file)
            break
except ImportError:
    pass

from app.core.config import settings  # noqa: E402
# Sprint 4 收尾：core/embedding.py 改为 Provider 抽象入口
from app.core.providers.embedding import get_embedding_provider  # noqa: E402
from app.clients.qdrant import search as qdrant_search  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_INPUT = PROJECT_ROOT / "data" / "eval_set_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "eval_hitk_report.json"
TOP_K_MAX = 10
# Rerank 模式：先取 top-RERANK_K 候选，rerank 后取 top-TOP_K_MAX
# 限制在 15 以内（rerank.py 单 prompt token 上限）
RERANK_K = 15


# =============================================================
# 1. 加载评估集
# =============================================================
def load_eval_set(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"评估集不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"评估集格式错误，期望 list，实际 {type(data)}")
    for item in data:
        if "query" not in item or "relevant_doc_id" not in item:
            raise ValueError(f"评估集项缺少必要字段: {item}")
    return data


# =============================================================
# 2. 单条评估
# =============================================================
def evaluate_single(
    item: Dict[str, str],
    use_rerank: bool = False,
    use_bm25: bool = False,
    use_multi_query: bool = False,
) -> Dict[str, Any]:
    """
    对单条 query 做 embedding + Qdrant top-K 检索，记录结果

    Args:
        item: 评估集单项
        use_rerank: 是否启用 LLM cross-encoder rerank
        use_bm25: 是否启用 BM25 稀疏召回 + RRF 融合
        use_multi_query: 是否启用 Phase 4 A4 Multi-Query（query_rewriter + policy 多路）

    Returns:
        {
            "query": str,
            "relevant_doc_id": str,
            "retrieved_ids": [str, ...],
            "rank": int | None,  # relevant_doc_id 排在第几位（None = 没找到）
            "latency_ms": float,
            "hit_at_k": {1: bool, 3: bool, 5: bool, 10: bool}
        }
    """
    query = item["query"]
    relevant_id = item["relevant_doc_id"]

    t0 = time.perf_counter()

    # Phase 4 A4：Multi-Query 路径（生成 N 路 query → 多路 RAG → RRF 融合）
    if use_multi_query:
        try:
            from app.services.query_rewriter import rewrite_query_multi
            from app.services.policy_service import PolicyService
            queries, was_rewritten = rewrite_query_multi(query, n=3)
            results = PolicyService.search_multi_policy(queries, top_k=TOP_K_MAX)
        except Exception as e:
            logger.warning(f"multi_query 检索失败，降级到 hybrid: {e}")
            use_multi_query = False  # 降级到 hybrid 路径

    if not use_multi_query:
        query_vector = get_embedding_provider().embed_text(query)

        # 候选路选择
        if use_bm25:
            candidates = qdrant_search(query_vector, top_k=RERANK_K)
            try:
                from app.services.bm25_index import bm25_search
                from app.services.rrf import rrf_fuse
                bm25_hits = bm25_search(query, top_k=RERANK_K)
                if bm25_hits:
                    fused = rrf_fuse([candidates, bm25_hits], k=60)
                    candidates = fused[:RERANK_K]
                else:
                    logger.debug(f"BM25 空结果，仅用 dense candidates: query={query!r}")
            except Exception as e:
                logger.warning(f"BM25 检索失败，降级到纯 dense: {e}")
        else:
            candidates = qdrant_search(query_vector, top_k=RERANK_K)

        if use_rerank:
            from app.core.providers.rerank import get_rerank_provider
            results = get_rerank_provider().rerank(query, candidates, top_n=TOP_K_MAX)
        else:
            results = candidates[:TOP_K_MAX]

    latency_ms = (time.perf_counter() - t0) * 1000

    retrieved_ids = [r["id"] for r in results]
    rank = None
    for idx, rid in enumerate(retrieved_ids, 1):
        if rid == relevant_id:
            rank = idx
            break

    hit_at_k = {k: (rank is not None and rank <= k) for k in (1, 3, 5, 10)}
    return {
        "query": query,
        "relevant_doc_id": relevant_id,
        "source": item.get("source", "unknown"),
        "retrieved_ids": retrieved_ids,
        "rank": rank,
        "latency_ms": round(latency_ms, 2),
        "hit_at_k": hit_at_k,
    }


# =============================================================
# 3. 汇总统计
# =============================================================
def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(results)
    if n == 0:
        return {}

    hit_rates = {k: sum(r["hit_at_k"][k] for r in results) / n for k in (1, 3, 5, 10)}
    latencies = [r["latency_ms"] for r in results]

    # 按 source 分组（看哪些类目召回差）
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_source.setdefault(r["source"], []).append(r)

    source_stats = {}
    for src, items in by_source.items():
        m = len(items)
        source_stats[src] = {
            "count": m,
            "hit@1": round(sum(x["hit_at_k"][1] for x in items) / m, 3),
            "hit@5": round(sum(x["hit_at_k"][5] for x in items) / m, 3),
        }

    # 失败案例
    misses = [r for r in results if r["rank"] is None]
    miss_samples = [
        {"query": m["query"], "relevant_id": m["relevant_doc_id"], "retrieved_top3": m["retrieved_ids"][:3]}
        for m in misses[:10]
    ]

    return {
        "total": n,
        "hit@1": round(hit_rates[1], 3),
        "hit@3": round(hit_rates[3], 3),
        "hit@5": round(hit_rates[5], 3),
        "hit@10": round(hit_rates[10], 3),
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1),
            "p90": round(sorted(latencies)[int(len(latencies) * 0.9)], 1),
            "max": round(max(latencies), 1),
        },
        "miss_count": len(misses),
        "miss_rate": round(len(misses) / n, 3),
        "by_source": source_stats,
        "miss_samples": miss_samples,
    }


# =============================================================
# 4. 打印报告
# =============================================================
def print_report(summary: Dict[str, Any]) -> None:
    print("\n" + "=" * 70)
    print("hit@K 评估报告")
    print("=" * 70)
    print(f"  评估集大小:   {summary['total']} 条 query")
    print()
    print("  全局召回率：")
    print(f"    hit@1:   {summary['hit@1']:.3f}  ({int(summary['hit@1'] * summary['total']):>3}/{summary['total']})")
    print(f"    hit@3:   {summary['hit@3']:.3f}  ({int(summary['hit@3'] * summary['total']):>3}/{summary['total']})")
    print(f"    hit@5:   {summary['hit@5']:.3f}  ({int(summary['hit@5'] * summary['total']):>3}/{summary['total']})")
    print(f"    hit@10:  {summary['hit@10']:.3f}  ({int(summary['hit@10'] * summary['total']):>3}/{summary['total']})")
    print()
    print("  检索时延（ms）：")
    print(f"    p50:   {summary['latency_ms']['p50']}")
    print(f"    p90:   {summary['latency_ms']['p90']}")
    print(f"    max:   {summary['latency_ms']['max']}")
    print()
    print("  按 source 分组（hit@1 / hit@5）：")
    print(f"    {'source':<40} {'n':>4}  {'hit@1':>6}  {'hit@5':>6}")
    print("    " + "-" * 60)
    for src, stat in sorted(summary["by_source"].items(), key=lambda x: -x[1]["count"]):
        print(f"    {src:<40} {stat['count']:>4}  {stat['hit@1']:>6.3f}  {stat['hit@5']:>6.3f}")
    print()
    if summary["miss_samples"]:
        print(f"  失败案例（前 {len(summary['miss_samples'])} 条，relevant_doc 完全没进 top-10）：")
        for m in summary["miss_samples"]:
            print(f"    Q: {m['query']}")
            print(f"       relevant:  {m['relevant_id']}")
            print(f"       retrieved: {m['retrieved_top3']}")
    print("=" * 70)


# =============================================================
# 5. 主流程
# =============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="hit@K 评估")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="评估集 JSON 路径")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="详细结果输出")
    parser.add_argument("--rerank", action="store_true", help="启用 LLM cross-encoder rerank（top-20→rerank→top-10）")
    parser.add_argument("--bm25", action="store_true", help="启用 BM25 稀疏召回 + RRF 融合（与 --rerank 可叠加）")
    parser.add_argument("--multi-query", action="store_true", help="启用 Phase 4 A4 Multi-Query（query_rewriter 输出 N 路 + policy 多路 RRF 融合）")
    parser.add_argument("--latency-bench", action="store_true", help="latency benchmark 模式：每条 query 跑 3 次取中位数（防抖动）")
    args = parser.parse_args()

    if args.multi_query:
        mode = "multi_query"
    elif args.bm25 and args.rerank:
        mode = "hybrid_rerank"
    elif args.bm25:
        mode = "hybrid"
    elif args.rerank:
        mode = "rerank"
    else:
        mode = "baseline"
    logger.info(f"加载评估集: {args.input}")
    eval_set = load_eval_set(args.input)
    logger.info(f"  共 {len(eval_set)} 条，模式={mode}")

    logger.info(f"开始评估（embedding model: text-embedding-v3, collection: {settings.QDRANT_COLLECTION}）")
    # B1.1：latency benchmark 模式 → 每条 query 跑 3 次取中位数
    n_runs = 3 if args.latency_bench else 1
    if args.latency_bench:
        logger.info(f"  latency-bench 模式：每条 query 跑 {n_runs} 次取中位数")
    results: List[Dict[str, Any]] = []
    for i, item in enumerate(eval_set, 1):
        try:
            if n_runs == 1:
                r = evaluate_single(item, use_rerank=args.rerank, use_bm25=args.bm25, use_multi_query=args.multi_query)
            else:
                # 跑 3 次取 latency_ms 中位数；其他字段（hit/rank/retrieved_ids）取最后一次
                latencies = []
                last_r = None
                for _ in range(n_runs):
                    last_r = evaluate_single(item, use_rerank=args.rerank, use_bm25=args.bm25, use_multi_query=args.multi_query)
                    latencies.append(last_r["latency_ms"])
                median_latency = round(statistics.median(latencies), 2)
                last_r["latency_ms"] = median_latency
                last_r["latency_bench_runs"] = n_runs
                last_r["latency_bench_raw"] = latencies
                r = last_r
            results.append(r)
        except Exception as e:
            logger.error(f"[{i}/{len(eval_set)}] query='{item['query']}' 评估失败: {e}")
            continue

        if i % 20 == 0 or i == len(eval_set):
            logger.info(f"  进度 [{i}/{len(eval_set)}]")

    if not results:
        logger.error("无任何成功结果")
        return 1

    summary = summarize(results)
    print_report(summary)

    # 写详细结果
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {"summary": summary, "details": results, "mode": mode},
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info(f"详细结果已写入: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
