"""多轮 Agent 评测（V20 亮点 5）：20 组多轮场景 × 3 维度离线评测。

评测维度（不依赖真实 LLM 调用，离线可跑、零 API 成本）：
1. 意图路由正确率  —— IntentService 规则层命中且意图 == 期望（method=rule/rule_short）
2. 实体抽取正确率  —— order_no / SKU 正则是否抽到期望值（工具选择的代理指标）
3. 规则层覆盖率    —— 多轮短句有多少轮规则层 miss（需 LLM 兜底）——
   这个数字**不是为了好看**：覆盖率低说明多轮场景对 LLM 依赖重，
   是真实的成本与延迟信号，如实上报，不允许为了让分数好看去改期望。

原则（与 60 题回归集一致）：期望值按"买家这句话业务上应该路由到哪"写死；
跑出来不对 = 真缺陷，记录进报告，禁止反向改期望对齐结果。

用法：
    PYTHONPATH=backend python scripts/eval_multiturn.py                # 跑评测打报告
    PYTHONPATH=backend python scripts/eval_multiturn.py --out docs/reports/eval-multiturn-latest.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path


def load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def eval_case_offline(case: dict, intent_mod) -> dict:
    """离线跑一个多轮场景：只走规则层（不调 LLM）。

    返回逐轮结果 + 场景级汇总。
    """
    turn_results = []
    for turn in case["turns"]:
        query = turn["query"]
        expect = turn["expect"]

        # 实体抽取（与 classify 入口同一套正则）
        entities = {"order_no": None, "sku": None, "keywords": []}
        m = intent_mod.ORDER_NO_RE.search(query)
        if m:
            entities["order_no"] = m.group(0)
        m = intent_mod.SKU_RE.search(query)
        if m:
            entities["sku"] = m.group(0)

        # 规则层意图（method=rule_short / rule / None=LLM 兜底区）
        hit = intent_mod.IntentService._short_or_rule(query, entities)
        if hit:
            intent = hit.get("intent") or hit.get("primary")
            method = hit.get("method", "rule")
        else:
            intent, method = None, "llm_fallback"

        exp_intent = expect.get("intent")
        exp_entities = expect.get("entities") or {}
        intent_ok = (intent == exp_intent)
        entities_ok = True
        entity_detail = {}
        for field, expected_val in exp_entities.items():
            actual = entities.get(field)
            entity_detail[field] = {"expected": expected_val, "actual": actual}
            if actual != expected_val:
                entities_ok = False

        turn_results.append({
            "query": query,
            "expected_intent": exp_intent,
            "actual_intent": intent,
            "intent_ok": intent_ok,
            "intent_method": method,
            "entities_ok": entities_ok,
            "entity_detail": entity_detail,
        })

    turns = len(turn_results)
    intent_ok_n = sum(1 for t in turn_results if t["intent_ok"])
    entities_ok_n = sum(1 for t in turn_results if t["entities_ok"])
    rule_covered = sum(1 for t in turn_results if t["intent_method"] != "llm_fallback")
    return {
        "scenario_id": case["scenario_id"],
        "category": case["category"],
        "desc": case.get("desc", ""),
        "turns": turns,
        "intent_ok": intent_ok_n,
        "entities_ok": entities_ok_n,
        "rule_covered": rule_covered,
        "pass": (intent_ok_n == turns and entities_ok_n == turns),
        "turn_results": turn_results,
    }


def summarize(results: list[dict]) -> dict:
    total_turns = sum(r["turns"] for r in results)
    intent_ok = sum(r["intent_ok"] for r in results)
    entities_ok = sum(r["entities_ok"] for r in results)
    rule_covered = sum(r["rule_covered"] for r in results)
    passed_scenarios = sum(1 for r in results if r["pass"])
    pct = lambda n: round(n / total_turns * 100, 1) if total_turns else 0.0
    return {
        "scenarios": len(results),
        "scenarios_passed": passed_scenarios,
        "turns": total_turns,
        "intent_accuracy": pct(intent_ok),
        "entities_accuracy": pct(entities_ok),
        "rule_coverage": pct(rule_covered),
    }


def render_report(results: list[dict], summary: dict) -> str:
    lines = [
        "# 多轮 Agent 评测报告（V20 亮点 5 · eval_multiturn_v1 离线）",
        "",
        f"> 场景 {summary['scenarios']} 组 / {summary['turns']} 轮；通过 {summary['scenarios_passed']} 组",
        "",
        "## 指标",
        "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
        f"| 意图路由正确率 | **{summary['intent_accuracy']}%** | 规则层命中且意图符合期望 |",
        f"| 实体抽取正确率 | **{summary['entities_accuracy']}%** | order_no / SKU 正则抽到期望值 |",
        f"| 规则层覆盖率 | {summary['rule_coverage']}% | 多轮短句规则可答比例；其余需 LLM 兜底 |",
        "",
        "## 场景明细",
        "",
        "| 场景 | 类别 | 轮数 | 意图√ | 实体√ | 规则覆盖 | 通过 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['scenario_id']} {r['desc'][:14]} | {r['category']} | {r['turns']} "
            f"| {r['intent_ok']}/{r['turns']} | {r['entities_ok']}/{r['turns']} "
            f"| {r['rule_covered']}/{r['turns']} | {'✅' if r['pass'] else '❌'} |"
        )
    lines += ["", "## 失败轮明细（期望不改，缺陷如实记录）", ""]
    for r in results:
        for t in r["turn_results"]:
            if not t["intent_ok"] or not t["entities_ok"]:
                lines.append(
                    f"- **{r['scenario_id']}** 「{t['query']}」期望 {t['expected_intent']}，"
                    f"实际 {t['actual_intent']}（{t['intent_method']}）"
                    + (f"；实体: {t['entity_detail']}" if not t["entities_ok"] else "")
                )
    return "\n".join(lines) + "\n"


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

    parser = argparse.ArgumentParser(description="多轮 Agent 评测（离线规则层）")
    parser.add_argument("--out", help="Markdown 报告输出路径")
    parser.add_argument("--cases", help="评测集路径（默认 <repo>/data/eval_multiturn_v1.json）")
    args = parser.parse_args()

    import app.services.intent_service as intent_mod

    root = Path(__file__).resolve().parents[1]
    cases_path = Path(args.cases) if args.cases else root / "data" / "eval_multiturn_v1.json"
    cases = load_cases(cases_path)

    t0 = time.perf_counter()
    results = [eval_case_offline(c, intent_mod) for c in cases]
    summary = summarize(results)
    summary["elapsed_s"] = round(time.perf_counter() - t0, 2)

    report = render_report(results, summary)
    print(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"[saved] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
