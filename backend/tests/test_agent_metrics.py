"""业务价值指标测试（V20 亮点 4：collect_trace_metrics 纯函数）"""
from __future__ import annotations

from app.services.chat.agent_metrics import collect_trace_metrics, render_markdown


def _row(outcome="ok", degraded=False, cache_hit=False, method="llm",
         intent="policy_query", latency=1000.0):
    return {
        "session_id": "s", "user_id": 1, "channel": "web",
        "outcome": outcome, "degraded": degraded, "cache_hit": cache_hit,
        "intent": intent, "intent_method": method, "latency_ms": latency,
    }


def test_empty_rows():
    m = collect_trace_metrics([])
    assert m == {"total": 0}
    assert "暂无数据" in render_markdown(m)


def test_core_rates():
    rows = [
        _row(outcome="ok", method="rule"),                       # 自动 + 规则
        _row(outcome="ok", cache_hit=True, method="cache_hit"),  # 自动 + 缓存
        _row(outcome="handoff_p0", degraded=True),               # 转人工
        _row(outcome="handoff_user", degraded=True),             # 转人工
        _row(outcome="blocked"),                                 # 拦截
        _row(outcome="error"),
    ]
    m = collect_trace_metrics(rows)
    assert m["total"] == 6
    assert m["auto_resolve_rate"] == 33.3     # 2/6
    assert m["handoff_rate"] == 33.3          # 2/6
    assert m["blocked_rate"] == 16.7          # 1/6
    assert m["cache_hit_rate"] == 16.7
    assert m["rule_route_rate"] == 16.7
    assert m["error_rate"] == 16.7


def test_latency_percentiles():
    rows = [_row(latency=float(i + 1) * 100) for i in range(100)]  # 100..10000ms
    m = collect_trace_metrics(rows)
    assert m["p50_latency_ms"] == 5000.0     # 第 50 小 = 50*100
    assert m["p95_latency_ms"] == 9500.0     # ceil(0.95*100)=95 → 9500


def test_intent_top_sorted():
    rows = [_row(intent="refund_query") for _ in range(5)]
    rows += [_row(intent="policy_query") for _ in range(3)]
    rows += [_row(intent="product_query") for _ in range(2)]
    m = collect_trace_metrics(rows)
    top = m["intent_top"]
    assert top[0] == ("refund_query", 5)
    assert top[1] == ("policy_query", 3)
    assert top[2] == ("product_query", 2)


def test_markdown_renders_boss_metrics():
    m = collect_trace_metrics([_row(outcome="ok"), _row(outcome="handoff_p0", degraded=True)])
    md = render_markdown(m)
    assert "自动处理率" in md and "50.0%" in md
    assert "转人工率" in md
    assert "P50" in md


def test_dirty_rows_ignored():
    """非 dict 行 / 缺字段行不炸。"""
    rows = [
        {"outcome": "ok"},
        "not-a-dict",
        {"latency_ms": "bad-type"},  # latency 非数字被过滤
        {"outcome": "blocked"},
    ]
    m = collect_trace_metrics(rows)
    assert m["total"] == 3  # 非 dict 行被过滤
    assert m["p50_latency_ms"] == 0.0
