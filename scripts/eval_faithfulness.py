"""
eval_faithfulness.py - 评估 RAG 生成答案的忠实度（轻量化）

评估 3 指标：
- citation_rate：答案是否引用 ≥1 个 expected_keywords（规则）
- no_hallucination_rate：答案是否出现 retrieve top-K 外的敏感实体（规则）
- faithfulness_score = citation_rate * no_hallucination_rate（综合）

轻量 LLM-as-judge（兜底）：
- 仅当答案 < 10 字 或 expected_keywords 抽取为空时触发
- 50 token 内输出 1/0 二值；复用现有 get_llm_provider()，不开新调用

输入：
- data/eval_faith_set.json（query / expected_keywords / sensitive_keywords / retrieve_top_k_text）

设计取舍：
- 纯规则优先（覆盖 ~80% 场景）：substring 匹配 expected_keywords
- mini-judge 兜底（覆盖 ~20% 场景）：50 token，max_tokens=5
- 总成本：100 条 query ≈ 1000 token（vs 完整 LLM-as-judge 20000 token，-95%）

用法：
    PYTHONPATH=backend python scripts/eval_faithfulness.py
    # 或指定不同的评测集
    PYTHONPATH=backend python scripts/eval_faithfulness.py --input data/eval_faith_set_v2.json
"""
import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 加载 .env（与 eval_hitk.py 一致）
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_INPUT = PROJECT_ROOT / "data" / "eval_faith_set.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "eval_faithfulness_report.json"
MINI_JUDGE_MAX_TOKENS = 5
MIN_ANSWER_LEN_FOR_RULE = 10  # 答案 < 10 字走 mini-judge
SKU_PATTERN = re.compile(r"\bSKU\d+|\bZP\d+|\bORD\d+", re.IGNORECASE)
NUM_PATTERN = re.compile(r"\d+(?:\.\d+)?")


# =============================================================
# 1. 加载评测集
# =============================================================
def load_faith_set(path: Path) -> List[Dict[str, Any]]:
    """加载忠实度评测集

    每条格式：
    {
        "query": "...",
        "expected_keywords": ["运费险", "7天", ...],
        "sensitive_keywords": ["全额退款", "免运费", ...],  # 不应出现的实体
        "retrieve_top_k_text": "...",  # 检索到的 top-K 拼接文本（用于上下文校验）
        "source": "policy_shipping_main"
    }
    """
    if not path.exists():
        raise FileNotFoundError(f"忠实度评测集不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"评测集格式错误，期望 list，实际 {type(data)}")
    for item in data:
        if "query" not in item:
            raise ValueError(f"评测集项缺少必要字段 query: {item}")
        # 缺字段时给空默认值（不阻断运行）
        item.setdefault("expected_keywords", [])
        item.setdefault("sensitive_keywords", [])
        item.setdefault("retrieve_top_k_text", "")
        item.setdefault("source", "unknown")
    return data


# =============================================================
# 2. 实体抽取（轻量规则）
# =============================================================
def extract_entities(text: str) -> Dict[str, List[str]]:
    """从文本中抽取结构化实体

    Returns:
        {
            "skus": [...],      # SKU010 / ZP1 / ORD20240101
            "numbers": [...],   # 7 / 7.5 / 100
        }
    """
    return {
        "skus": [m.group(0) for m in SKU_PATTERN.finditer(text)],
        "numbers": [m.group(0) for m in NUM_PATTERN.finditer(text)][:10],  # 截前 10 个防爆
    }


# =============================================================
# 3. 单条忠实度评分（规则 + mini-judge 兜底）
# =============================================================
def score_faithfulness_rule(
    query: str,
    answer: str,
    expected_keywords: List[str],
    sensitive_keywords: List[str],
    retrieve_top_k_text: str,
) -> Dict[str, Any]:
    """规则路径：纯字符串匹配（80% 场景）

    Returns:
        {
            "citation_count": int,
            "citation_rate": 0.0 | 1.0,
            "hallucinated_entities": [...],
            "no_hallucination_rate": 0.0 | 0.5 | 1.0,
            "score": float,  # citation_rate * no_hallucination_rate
            "method": "rule",
        }
    """
    # citation_rate
    if expected_keywords:
        citation_count = sum(1 for kw in expected_keywords if kw in answer)
        citation_rate = 1.0 if citation_count > 0 else 0.0
    else:
        # 没给 expected_keywords → 视为中性（score=0.5，不拉低也不拉高）
        citation_count = 0
        citation_rate = 0.5

    # no_hallucination_rate（基于 sensitive_keywords）
    hallucinated_entities: List[str] = []
    if sensitive_keywords:
        for kw in sensitive_keywords:
            if kw in answer:
                hallucinated_entities.append(kw)
        if hallucinated_entities:
            no_hallucination_rate = 0.0
        else:
            no_hallucination_rate = 1.0
    else:
        # 没给 sensitive_keywords → 用 retrieve top-K 实体校验
        # 答案中出现 retrieve top-K 没有的 SKU/数字 → 疑似幻觉
        answer_entities = extract_entities(answer)
        retrieve_entities = extract_entities(retrieve_top_k_text)
        all_retrieve_skus = set(retrieve_entities["skus"])
        answer_skus = set(answer_entities["skus"])

        # 答案中出现的 SKU 不在 retrieve top-K → 疑似幻觉
        suspicious_skus = answer_skus - all_retrieve_skus
        if suspicious_skus:
            hallucinated_entities = list(suspicious_skus)
            no_hallucination_rate = 0.5
        else:
            no_hallucination_rate = 1.0

    score = citation_rate * no_hallucination_rate
    return {
        "citation_count": citation_count,
        "citation_rate": citation_rate,
        "hallucinated_entities": hallucinated_entities,
        "no_hallucination_rate": no_hallucination_rate,
        "score": score,
        "method": "rule",
    }


def score_faithfulness_mini_judge(
    query: str,
    answer: str,
    retrieve_top_k_text: str,
) -> Dict[str, Any]:
    """轻量 LLM-as-judge 兜底（20% 场景）

    仅当规则置信度低时调用：答案 < 10 字 或 expected_keywords 空
    50 token 内输出 1/0；复用现有 get_llm_provider()

    Returns:
        {"score": 0.0 | 1.0, "method": "mini_judge", "raw_judge": "1"}
    """
    try:
        from app.core.providers.llm import get_llm_provider  # noqa: E402

        # 截断 retrieve 到 200 字避免 prompt 过长
        retrieve_preview = retrieve_top_k_text[:200]
        prompt = (
            f"判断答案是否引用了检索内容。仅回答 1（引用）或 0（未引用）。\n"
            f"问题：{query}\n答案：{answer}\n检索：{retrieve_preview}"
        )
        result = get_llm_provider().chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=MINI_JUDGE_MAX_TOKENS,
        )
        raw = result.get("reply", "").strip()
        score = 1.0 if "1" in raw else 0.0
        return {"score": score, "method": "mini_judge", "raw_judge": raw}
    except Exception as e:
        logger.warning(f"mini-judge 调用失败，降级为 0.5（中性）: {e}")
        return {"score": 0.5, "method": "mini_judge_fallback", "error": str(e)}


def score_faithfulness(
    item: Dict[str, Any],
    answer: str,
) -> Dict[str, Any]:
    """单条忠实度评分（决策规则路径还是 mini-judge 路径）

    决策逻辑：
    - 答案 < 10 字 → mini-judge（短答案规则不准）
    - expected_keywords 为空 → mini-judge（无引用目标）
    - 否则 → 规则路径
    """
    query = item["query"]
    expected = item.get("expected_keywords", [])
    sensitive = item.get("sensitive_keywords", [])
    retrieve_text = item.get("retrieve_top_k_text", "")

    if len(answer) < MIN_ANSWER_LEN_FOR_RULE or not expected:
        # 走 mini-judge
        result = score_faithfulness_mini_judge(query, answer, retrieve_text)
        result["path"] = "mini_judge"
    else:
        # 走规则
        result = score_faithfulness_rule(query, answer, expected, sensitive, retrieve_text)
        result["path"] = "rule"

    result["query"] = query
    result["answer"] = answer
    result["source"] = item.get("source", "unknown")
    return result


# =============================================================
# 4. 汇总统计
# =============================================================
def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总：总平均 + 按 path 分组 + 按 source 分组 + 失败案例"""
    n = len(results)
    if n == 0:
        return {}

    avg_score = sum(r["score"] for r in results) / n

    # 按 path 分组（看规则 vs mini-judge 占比）
    by_path: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_path.setdefault(r["path"], []).append(r)

    path_stats = {}
    for path, items in by_path.items():
        m = len(items)
        path_stats[path] = {
            "count": m,
            "ratio": round(m / n, 3),
            "avg_score": round(sum(x["score"] for x in items) / m, 3),
        }

    # 按 source 分组
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_source.setdefault(r["source"], []).append(r)

    source_stats = {}
    for src, items in by_source.items():
        m = len(items)
        source_stats[src] = {
            "count": m,
            "avg_score": round(sum(x["score"] for x in items) / m, 3),
        }

    # 低分案例（score < 0.5，前 10 条）
    low_score = sorted(results, key=lambda r: r["score"])[:10]
    low_samples = [
        {
            "query": r["query"],
            "source": r["source"],
            "path": r["path"],
            "score": r["score"],
            "hallucinated_entities": r.get("hallucinated_entities", []),
            "answer_preview": r.get("answer", "")[:80],
        }
        for r in low_score
        if r["score"] < 0.5
    ]

    return {
        "total": n,
        "avg_score": round(avg_score, 3),
        "by_path": path_stats,
        "by_source": source_stats,
        "low_score_samples": low_samples,
    }


# =============================================================
# 5. 打印报告
# =============================================================
def print_report(summary: Dict[str, Any]) -> None:
    print("\n" + "=" * 70)
    print("Faithfulness 评估报告（轻量化）")
    print("=" * 70)
    print(f"  评测集大小:   {summary['total']} 条")
    print(f"  综合 Faithfulness Score:   {summary['avg_score']:.3f}")
    print()
    print(f"  按路径分组（rule vs mini-judge）：")
    print(f"    {'path':<20} {'count':>5}  {'ratio':>6}  {'avg_score':>9}")
    print("    " + "-" * 50)
    for path, stat in sorted(summary["by_path"].items(), key=lambda x: -x[1]["count"]):
        print(
            f"    {path:<20} {stat['count']:>5}  "
            f"{stat['ratio']:>6.3f}  {stat['avg_score']:>9.3f}"
        )
    print()
    print(f"  按 source 分组（avg_score）：")
    print(f"    {'source':<40} {'n':>4}  {'avg_score':>9}")
    print("    " + "-" * 60)
    for src, stat in sorted(summary["by_source"].items(), key=lambda x: -x[1]["count"]):
        print(f"    {src:<40} {stat['count']:>4}  {stat['avg_score']:>9.3f}")
    print()
    if summary["low_score_samples"]:
        print(f"  低分案例（前 {len(summary['low_score_samples'])} 条，score < 0.5）：")
        for s in summary["low_score_samples"]:
            print(f"    Q: {s['query']}")
            print(f"       source={s['source']} path={s['path']} score={s['score']}")
            if s["hallucinated_entities"]:
                print(f"       幻觉实体: {s['hallucinated_entities']}")
            print(f"       答案: {s['answer_preview']}...")
    print("=" * 70)


# =============================================================
# 6. 主流程
# =============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 答案忠实度评测（轻量化）")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="评测集 JSON 路径")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="详细结果输出")
    parser.add_argument("--answers-file", type=Path, default=None,
                        help="已有答案 JSON 文件（query → answer）；缺省时用 placeholder 触发 mini-judge 演示")
    parser.add_argument("--demo", action="store_true",
                        help="演示模式：用 placeholder 答案触发 mini-judge，不依赖外部 API")
    args = parser.parse_args()

    logger.info(f"加载忠实度评测集: {args.input}")
    faith_set = load_faith_set(args.input)
    logger.info(f"  共 {len(faith_set)} 条")

    # 加载答案（演示模式用 placeholder）
    answers_map: Dict[str, str] = {}
    if args.answers_file:
        if not args.answers_file.exists():
            logger.error(f"答案文件不存在: {args.answers_file}")
            return 1
        with open(args.answers_file, "r", encoding="utf-8") as f:
            answers_data = json.load(f)
        if isinstance(answers_data, list):
            answers_map = {a["query"]: a["answer"] for a in answers_data if "query" in a and "answer" in a}
        elif isinstance(answers_data, dict):
            answers_map = answers_data
        logger.info(f"  加载 {len(answers_map)} 条答案")

    logger.info("开始评估忠实度（轻量化：规则优先 + mini-judge 兜底）")
    results: List[Dict[str, Any]] = []
    for i, item in enumerate(faith_set, 1):
        query = item["query"]
        answer = answers_map.get(query, "")

        if not answer:
            if args.demo:
                # 演示模式：用 placeholder 答案
                answer = f"（演示答案）关于{query}的回复"
            else:
                logger.warning(f"[{i}/{len(faith_set)}] query='{query}' 无答案且非 demo 模式，跳过")
                continue

        try:
            r = score_faithfulness(item, answer)
            results.append(r)
        except Exception as e:
            logger.error(f"[{i}/{len(faith_set)}] query='{query}' 评估失败: {e}")
            continue

        if i % 10 == 0 or i == len(faith_set):
            logger.info(f"  进度 [{i}/{len(faith_set)}] 成功 {len(results)} 条")

    if not results:
        logger.error("无任何成功结果")
        return 1

    summary = summarize(results)
    print_report(summary)

    # 写详细结果
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {"summary": summary, "details": results},
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info(f"详细结果已写入: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())