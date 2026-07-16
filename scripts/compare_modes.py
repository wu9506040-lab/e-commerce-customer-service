"""
compare_modes.py - 一键跑 6 模式 RAG 检索 A/B 对比，输出对比报告

支持 6 模式（与 eval_hitk.py 一致）：
- baseline：纯 dense embedding + Qdrant
- rerank：dense + LLM cross-encoder rerank
- bm25：dense + BM25 + RRF 融合
- hybrid：dense + BM25 + RRF + rerank
- multi_query：query_rewriter 多路 + RRF 融合（Phase 4 A4）
- fuse_first_rerank：多路粗排 + RRF + 1 次 rerank（Phase 4 A8）

输入：data/eval_set_v<N>.json（与 eval_hitk.py 共享）
输出：data/eval_compare_report.json + 控制台对比表

设计取舍：
- 顺序执行（避免线程安全 + LLM Provider 限流）
- 每个模式独立调用 evaluate_single（复用 eval_hitk.py 内部函数）
- 报告按 hit@5 降序排，便于一眼看效果
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 复用 eval_hitk 的核心函数
from scripts.eval_hitk import (
    load_eval_set,
    evaluate_single,
    summarize,
    TOP_K_MAX,
    DEFAULT_INPUT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "eval_compare_report.json"

# 6 模式清单（baseline → 最复杂）
MODES = [
    {"name": "baseline",        "use_rerank": False, "use_bm25": False, "use_multi_query": False},
    {"name": "rerank",          "use_rerank": True,  "use_bm25": False, "use_multi_query": False},
    {"name": "bm25",            "use_rerank": False, "use_bm25": True,  "use_multi_query": False},
    {"name": "hybrid",          "use_rerank": True,  "use_bm25": True,  "use_multi_query": False},
    {"name": "multi_query",     "use_rerank": True,  "use_bm25": False, "use_multi_query": True},
    {"name": "fuse_first_rerank", "use_rerank": False, "use_bm25": False, "use_multi_query": True},
    # 注：fuse_first_rerank 是 multi_query + 关闭 per-query rerank（走 search_policy_coarse + 1 次 rerank）
    # 由 evaluate_single + ENABLE_MULTI_QUERY_FUSE_FIRST_RERANK=True 控制
]


# =============================================================
# 1. 单模式评测（包装 eval_hitk.evaluate_single）
# =============================================================
def evaluate_mode(eval_set: List[Dict[str, str]], mode_config: Dict[str, Any]) -> Dict[str, Any]:
    """跑单个模式的完整评测

    Args:
        eval_set: 评测集
        mode_config: {name, use_rerank, use_bm25, use_multi_query}

    Returns:
        {"name": str, "summary": Dict, "elapsed_sec": float, "failed": int}
    """
    name = mode_config["name"]
    logger.info(f"  [{name}] 开始评估...")

    # fuse_first_rerank 特殊处理：开启环境变量走 A8 路径
    import os
    env_backup = os.environ.get("ENABLE_MULTI_QUERY_FUSE_FIRST_RERANK")
    if name == "fuse_first_rerank":
        os.environ["ENABLE_MULTI_QUERY_FUSE_FIRST_RERANK"] = "true"

    t0 = time.perf_counter()
    results: List[Dict[str, Any]] = []
    failed = 0

    try:
        for i, item in enumerate(eval_set, 1):
            try:
                r = evaluate_single(
                    item,
                    use_rerank=mode_config["use_rerank"],
                    use_bm25=mode_config["use_bm25"],
                    use_multi_query=mode_config["use_multi_query"],
                )
                results.append(r)
            except Exception as e:
                failed += 1
                logger.warning(f"  [{name}] [{i}/{len(eval_set)}] 失败: {e}")
                continue

            if i % 50 == 0 or i == len(eval_set):
                logger.info(f"  [{name}] 进度 [{i}/{len(eval_set)}] 成功 {len(results)}")

        summary = summarize(results) if results else {}
        elapsed_sec = round(time.perf_counter() - t0, 2)

        return {
            "name": name,
            "config": mode_config,
            "summary": summary,
            "elapsed_sec": elapsed_sec,
            "failed": failed,
            "result_count": len(results),
        }
    finally:
        # 恢复环境变量
        if env_backup is None:
            os.environ.pop("ENABLE_MULTI_QUERY_FUSE_FIRST_RERANK", None)
        else:
            os.environ["ENABLE_MULTI_QUERY_FUSE_FIRST_RERANK"] = env_backup


# =============================================================
# 2. 对比报告（控制台 + JSON）
# =============================================================
def print_compare_table(mode_results: List[Dict[str, Any]]) -> None:
    """打印 6 模式对比表（按 hit@5 降序）"""
    print("\n" + "=" * 90)
    print("RAG 检索模式 A/B 对比报告")
    print("=" * 90)

    # 按 hit@5 降序排
    sorted_results = sorted(
        mode_results,
        key=lambda r: r.get("summary", {}).get("hit@5", 0.0),
        reverse=True,
    )

    header = (
        f"  {'模式':<20} {'hit@1':>6} {'hit@3':>6} {'hit@5':>6} {'hit@10':>7}"
        f" {'p50(ms)':>9} {'p90(ms)':>9} {'miss%':>7} {'耗时(s)':>8}"
    )
    print(header)
    print("  " + "-" * 86)

    for r in sorted_results:
        s = r.get("summary", {})
        lat = s.get("latency_ms", {})
        print(
            f"  {r['name']:<20} "
            f"{s.get('hit@1', 0):>6.3f} "
            f"{s.get('hit@3', 0):>6.3f} "
            f"{s.get('hit@5', 0):>6.3f} "
            f"{s.get('hit@10', 0):>7.3f} "
            f"{lat.get('p50', 0):>9.1f} "
            f"{lat.get('p90', 0):>9.1f} "
            f"{s.get('miss_rate', 0):>7.3f} "
            f"{r.get('elapsed_sec', 0):>8.2f}"
        )
    print("=" * 90)

    # 推荐建议
    if sorted_results:
        best = sorted_results[0]
        baseline = next((r for r in sorted_results if r["name"] == "baseline"), None)
        print()
        print(f"  推荐：{best['name']}（hit@5={best['summary'].get('hit@5', 0):.3f}）")
        if baseline and baseline["name"] != best["name"]:
            delta = best["summary"].get("hit@5", 0) - baseline["summary"].get("hit@5", 0)
            print(f"  vs baseline 提升 hit@5: {delta:+.3f}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索模式 A/B 对比")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="评测集 JSON 路径")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="对比报告输出")
    parser.add_argument("--modes", nargs="+", default=None,
                        help="指定模式（默认全部 6 个）；可选: baseline / rerank / bm25 / hybrid / multi_query / fuse_first_rerank")
    args = parser.parse_args()

    logger.info(f"加载评测集: {args.input}")
    eval_set = load_eval_set(args.input)
    logger.info(f"  共 {len(eval_set)} 条")

    # 模式选择
    selected_modes = MODES
    if args.modes:
        selected_modes = [m for m in MODES if m["name"] in args.modes]
        if not selected_modes:
            logger.error(f"无效的模式: {args.modes}；可选: {[m['name'] for m in MODES]}")
            return 1
        logger.info(f"  选定 {len(selected_modes)} 个模式: {[m['name'] for m in selected_modes]}")

    logger.info(f"开始跑 {len(selected_modes)} 个模式（顺序执行，避免 LLM Provider 限流）")
    mode_results: List[Dict[str, Any]] = []
    for mode in selected_modes:
        result = evaluate_mode(eval_set, mode)
        mode_results.append(result)

    # 控制台对比表
    print_compare_table(mode_results)

    # 写 JSON 报告
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {
                "eval_set_size": len(eval_set),
                "modes": mode_results,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info(f"对比报告已写入: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())