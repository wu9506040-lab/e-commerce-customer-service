#!/usr/bin/env python
"""V14（2.5）意图黄金评测脚本——离线跑出"规则层覆盖 X%、分类别 P/R"硬数字

跑法（CI 可自动跑，本地离线也跑）：
    PYTHONPATH=backend python scripts/eval_intent_classify.py [--llm-stub primary]

默认 mock LLM 兜底永远返 primary（refund_query）——测**规则层覆盖率**；
    --llm-stub=correct 让 mock 返 correct，测规则漏掉时整体精度（LLM 兜底准确率）。

每条样本过 IntentService.classify，结果分两路：
- method=rule → 规则层匹配，看 expected 是否一致（精度）
- method=llm/default → LLM 兜底，对 mock 行为评估覆盖率

输出：
    控制台：5 类意图的 TP/FP/FN → P/R/F1 + 规则覆盖率表
    文件：data/intent_golden_report.json（机器可读）
"""
import argparse
import json
import os
import statistics
import sys
import unittest.mock
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4",
)

from app.services.golden_intent_set import GOLDEN_INTENT_SET, get_intent_summary
from app.services.intent_service import IntentService
from app.services.metrics import metrics


def run_eval(llm_stub: str = "primary") -> dict:
    """llm_stub=primary: mock LLM 永远返 'refund_query'（测规则层）+ 看兜底触发率；
    llm_stub=correct: mock LLM 永远返 expected（测整体上限）"""
    summary = get_intent_summary()
    all_intents = sorted(set(item["expected"] for item in GOLDEN_INTENT_SET))

    # 统计：expected → set of [TP/FP/FN counters per intent]
    correct_by_intent_method = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))  # intent → method → [TP, FP, FN]
    rule_layer_total = 0
    rule_layer_correct = 0
    rule_layer_match_by_difficulty: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_sample: List[dict] = []

    for item in GOLDEN_INTENT_SET:
        q = item["query"]
        expected = item["expected"]
        diff = item["difficulty"]
        try:
            with unittest.mock.patch.object(
                IntentService, "_llm_classify",
                new=lambda *a, **kw: {"primary": expected if llm_stub == "correct" else "refund_query",
                                       "method": "llm", "confidence": 0.9, "intents": [], "upgraded": False},
            ):
                result = IntentService.classify(q)
        except Exception as e:
            print(f"ERROR on query={q!r}: {e}")
            continue
        predicted = result.get("primary", "policy_query")
        method = result.get("method", "llm")
        is_correct = (predicted == expected)
        correct_by_intent_method[expected][method][0 if is_correct else 1] += 1
        if method == "rule":
            rule_layer_total += 1
            if is_correct:
                rule_layer_correct += 1
                correct_by_intent_method[expected]["rule"][0] += 1  # TP
                rule_layer_match_by_difficulty[diff]["hit"] += 1
            else:
                correct_by_intent_method[expected]["rule"][1] += 1  # FP（规则判错意图）
                rule_layer_match_by_difficulty[diff]["miss_match"] += 1
        else:
            # LLM 兜底：记 FP / FN
            if is_correct:
                correct_by_intent_method[expected]["llm"][1] += 1  # 兜底判对 = FP 里反向（保持简单：这里我们反着算 FN）
                # 修正：按方向，这里表示 llm 兜底命中正确意图的次数
            else:
                correct_by_intent_method[expected]["llm"][2] += 1  # 兜底判错
            rule_layer_match_by_difficulty[diff]["fallback"] += 1
        per_sample.append({"q": q, "expected": expected, "predicted": predicted,
                           "method": method, "diff": diff, "ok": is_correct})

    # 整理报告：按意图 P/R/F1
    intent_metrics: Dict[str, dict] = {}
    for intent in all_intents:
        cm = correct_by_intent_method[intent]
        rule_tp = cm["rule"][0]
        rule_fp = cm["rule"][1]
        rule_fn = sum(correct_by_intent_method[i_other]["rule"][1] for i_other in all_intents if i_other != intent)
        # llm 兜底：correct - wrong 算作命中正确意图
        llm_correct = cm["llm"][1]
        llm_wrong = cm["llm"][2]
        tp_total = rule_tp + llm_correct
        precision = tp_total / max(tp_total + rule_fp + llm_wrong, 1)
        recall = tp_total / max(rule_tp + rule_fn + llm_correct + llm_wrong, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-9)
        intent_metrics[intent] = {
            "rule_tp": rule_tp, "rule_fp": rule_fp, "rule_layer_match_total": rule_tp + rule_fp,
            "llm_correct": llm_correct, "llm_wrong": llm_wrong,
            "tp_total": tp_total, "precision": round(precision, 4),
            "recall": round(recall, 4), "f1": round(f1, 4),
        }

    rule_layer_coverage = rule_layer_total / max(len(per_sample), 1)  # 规则层实际触发率
    rule_layer_precision = rule_layer_correct / max(rule_layer_total, 1)  # 触发后判对的精度
    llm_fallback_rate = 1 - rule_layer_coverage  # LLM 兜底率
    overall_correct = sum(1 for s in per_sample if s["ok"])
    overall_acc = overall_correct / max(len(per_sample), 1)

    # 难度维度的规则层命中率
    diff_breakdown = {
        d: {
            "total": sum(rule_layer_match_by_difficulty[d].values()),
            "hit": rule_layer_match_by_difficulty[d].get("hit", 0),
            "miss_match": rule_layer_match_by_difficulty[d].get("miss_match", 0),
            "fallback": rule_layer_match_by_difficulty[d].get("fallback", 0),
            "rule_hit_rate": round(rule_layer_match_by_difficulty[d].get("hit", 0) /
                                  max(sum(rule_layer_match_by_difficulty[d].values()), 1), 4),
        }
        for d in ["easy", "medium", "hard", "adversarial"]
    }

    return {
        "llm_stub": llm_stub,
        "total_samples": len(per_sample),
        "overall_accuracy": round(overall_acc, 4),
        "rule_layer_total": rule_layer_total,
        "rule_layer_correct": rule_layer_correct,
        "rule_layer_coverage": round(rule_layer_coverage, 4),
        "rule_layer_precision": round(rule_layer_precision, 4),
        "llm_fallback_rate": round(llm_fallback_rate, 4),
        "by_intent": intent_metrics,
        "by_difficulty": diff_breakdown,
        "set_summary": summary,
    }


def render_table(report: dict) -> str:
    lines = []
    lines.append(f"\n=== Intent 黄金评测报告 (V14 2.5) ===")
    lines.append(f"llm_stub: {report['llm_stub']}")
    lines.append(f"总样本: {report['total_samples']}  总精度: {report['overall_accuracy']*100:.1f}%")
    lines.append(f"规则层覆盖率: {report['rule_layer_coverage']*100:.1f}% ({report['rule_layer_total']}/{report['total_samples']})  "
                 f"规则层精度: {report['rule_layer_precision']*100:.1f}%  LLM 兜底率: {report['llm_fallback_rate']*100:.1f}%")
    lines.append("")
    lines.append(f"{'意图':<14} {'总数':>6} {'rule_tp':>8} {'rule_fp':>8} {'llm✓':>6} {'llm✗':>6} {'P':>7} {'R':>7} {'F1':>7}")
    for intent, m in report["by_intent"].items():
        lines.append(
            f"{intent:<14} {m['rule_layer_match_total'] + m['llm_correct'] + m['llm_wrong']:>6} "
            f"{m['rule_tp']:>8} {m['rule_fp']:>8} {m['llm_correct']:>6} {m['llm_wrong']:>6} "
            f"{m['precision']*100:>6.1f}% {m['recall']*100:>6.1f}% {m['f1']*100:>6.1f}%"
        )
    lines.append("")
    lines.append(f"{'难度':<10} {'总数':>6} {'规则命中':>8} {'规则判错':>8} {'LLM兜底':>8} {'规则命中率':>10}")
    for d, m in report["by_difficulty"].items():
        lines.append(
            f"{d:<10} {m['total']:>6} {m['hit']:>8} {m['miss_match']:>8} {m['fallback']:>8} {m['rule_hit_rate']*100:>9.1f}%"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-stub", choices=("primary", "correct"), default="primary",
                    help="primary: mock LLM 永远返 'refund_query'（测规则层覆盖率）；"
                         "correct: mock LLM 永远返 expected（测整体上限）")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "data" / "intent_golden_report.json"))
    args = ap.parse_args()

    report = run_eval(args.llm_stub)
    print(render_table(report))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"\nREPORT -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())