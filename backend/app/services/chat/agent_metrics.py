"""业务价值指标（V20 亮点 4）：从 agent_trace JSONL 计算老板视角指标。

与亮点 3 的关系：agent_trace 每轮落一条 JSONL；本模块读这些行算指标——
自动处理率 / 转人工率 / 缓存命中率 / 规则路由命中率 / P50/P95 延迟 / 错误率。

为什么是"老板指标"：hit@3 是技术指标，"自动处理率 X%、每轮省一次 LLM 调用"
才是业务方听得懂的语言。README 第一屏与面试口径都从这里出数。

纯函数设计：collect_trace_metrics(rows) 不碰文件系统，测试直接喂合成行；
CLI（scripts/agent_metrics.py）只做读文件 + 渲染。
"""
from __future__ import annotations

import math
from typing import Any, Iterable


def _percent(n: int, total: int) -> float:
    return round(n / total * 100, 1) if total else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = min(len(vs) - 1, math.ceil(0.95 * len(vs)) - 1)
    return round(vs[idx], 1)


def _p50(values: list[float]) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = min(len(vs) - 1, math.ceil(0.50 * len(vs)) - 1)
    return round(vs[idx], 1)


def collect_trace_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """把 trace 行聚合成指标 dict（纯函数，不读文件）。"""
    rows = [r for r in rows if isinstance(r, dict)]
    total = len(rows)
    if total == 0:
        return {"total": 0}

    ok = sum(1 for r in rows if r.get("outcome") == "ok")
    degraded = sum(1 for r in rows if r.get("degraded") is True)
    blocked = sum(1 for r in rows if r.get("outcome") == "blocked")
    cache_hit = sum(1 for r in rows if r.get("cache_hit") is True)
    rule_routed = sum(1 for r in rows if r.get("intent_method") == "rule")
    errors = sum(1 for r in rows if r.get("outcome") in ("error", "cancelled"))
    handoffs = sum(1 for r in rows if str(r.get("outcome", "")).startswith("handoff"))

    latencies_all = [float(r["latency_ms"]) for r in rows if isinstance(r.get("latency_ms"), (int, float))]
    latencies_ok = [float(r["latency_ms"]) for r in rows
                    if r.get("outcome") == "ok" and isinstance(r.get("latency_ms"), (int, float))]

    # 意图分布（topN）
    intent_dist: dict[str, int] = {}
    for r in rows:
        it = r.get("intent") or "unknown"
        intent_dist[it] = intent_dist.get(it, 0) + 1
    intent_top = sorted(intent_dist.items(), key=lambda kv: -kv[1])[:8]

    return {
        "total": total,
        # —— 老板指标 ——
        "auto_resolve_rate": _percent(ok, total),          # 自动处理率：AI 独立答完
        "handoff_rate": _percent(handoffs, total),          # 转人工率
        "blocked_rate": _percent(blocked, total),           # 入口拦截率（省下的流量）
        "cache_hit_rate": _percent(cache_hit, total),       # 缓存命中率（省 LLM 钱）
        "rule_route_rate": _percent(rule_routed, total),    # 规则直答率（零 LLM 开销）
        "error_rate": _percent(errors, total),
        # —— 技术指标 ——
        "degraded_rate": _percent(degraded, total),
        "p50_latency_ms": _p50(latencies_all),
        "p95_latency_ms": _p95(latencies_all),
        "p50_latency_ok_ms": _p50(latencies_ok),
        "p95_latency_ok_ms": _p95(latencies_ok),
        "intent_top": intent_top,
    }


def render_markdown(m: dict[str, Any], title: str = "Agent 业务价值指标") -> str:
    """指标 → Markdown 报告（README 第一屏 / docs/reports 直接可用）。"""
    if m.get("total", 0) == 0:
        return f"# {title}\n\n暂无数据（agent_traces 为空）。\n"
    lines = [
        f"# {title}",
        "",
        f"> 样本：{m['total']} 轮对话（来自 agent_traces JSONL）",
        "",
        "## 业务价值",
        "",
        "| 指标 | 值 | 含义 |",
        "|---|---|---|",
        f"| 自动处理率 | **{m['auto_resolve_rate']}%** | AI 独立完成、未转人工未出错 |",
        f"| 转人工率 | {m['handoff_rate']}% | 人工接管（含用户主动要求） |",
        f"| 入口拦截率 | {m['blocked_rate']}% | 护栏拦掉垃圾/闲聊，未消耗任何模型调用 |",
        f"| 缓存命中率 | {m['cache_hit_rate']}% | 语义缓存直出，省整次生成 |",
        f"| 规则直答率 | {m['rule_route_rate']}% | 关键词命中零 LLM 开销 |",
        f"| 错误/中断率 | {m['error_rate']}% | error + cancelled |",
        "",
        "## 延迟",
        "",
        f"- 全路径 P50 / P95：{m['p50_latency_ms']}ms / {m['p95_latency_ms']}ms",
        f"- 成功轮 P50 / P95：{m['p50_latency_ok_ms']}ms / {m['p95_latency_ok_ms']}ms",
        "",
        "## 意图分布（Top）",
        "",
    ]
    for name, cnt in m.get("intent_top", []):
        lines.append(f"- {name}: {cnt}")
    return "\n".join(lines) + "\n"
