"""Agent 决策追踪测试（V20 亮点 3：AgentTrace JSONL）

覆盖：字段完整性 / mark_intent / 各 outcome 的 flush / 开关关闭 /
flush 异常不外抛（观测层不影响业务）/ JSONL 一行一条可回读。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.services.chat.agent_trace import AgentTrace


@pytest.fixture(autouse=True)
def _trace_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "AGENT_TRACE_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AGENT_TRACE_DIR", str(tmp_path / "traces"), raising=False)
    yield


def _read_lines(d: Path) -> list[dict]:
    files = sorted(d.rglob("trace-*.jsonl"))
    assert files, "trace 文件未生成"
    lines = []
    for f in files:
        lines.extend(json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())
    return lines


def test_basic_flush_fields():
    t = AgentTrace("sess-1", 7, "xianyu", "这个能退吗")
    t.mark_intent({"intent": "refund_query", "method": "rule", "confidence": 0.9})
    t.mark("degraded", True)
    t.flush("handoff_p0")

    rows = _read_lines(Path(settings.AGENT_TRACE_DIR))
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "sess-1"
    assert r["user_id"] == 7
    assert r["channel"] == "xianyu"
    assert r["query_len"] == 5  # "这个能退吗" 5 个字符
    assert r["intent"] == "refund_query"
    assert r["intent_method"] == "rule"
    assert r["intent_confidence"] == 0.9
    assert r["degraded"] is True
    assert r["outcome"] == "handoff_p0"
    assert isinstance(r["latency_ms"], (int, float))
    assert "ts" in r


def test_flush_only_once():
    """二次 flush 是 no-op（保证一轮只落一条）。"""
    t = AgentTrace("s", 1, None, "q")
    t.flush("ok")
    t.flush("error", error_type="X")
    rows = _read_lines(Path(settings.AGENT_TRACE_DIR))
    assert len(rows) == 1
    assert rows[0]["outcome"] == "ok"


def test_disabled_no_file(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_TRACE_ENABLED", False, raising=False)
    t = AgentTrace("s", 1, None, "q")
    t.flush("ok")
    assert not list(Path(settings.AGENT_TRACE_DIR).rglob("*.jsonl"))


def test_flush_never_raises(monkeypatch):
    """flush 里任何异常都必须吞掉——观测层绝不能影响应答。"""
    monkeypatch.setattr(settings, "AGENT_TRACE_DIR", "Z:/definitely/invalid/path", raising=False)
    t = AgentTrace("s", 1, None, "q")
    t.flush("ok")  # 不应抛异常
    # 再验证 mark 也不抛
    t.mark("anything", 1)


def test_outcomes_are_distinct_paths():
    for i, outcome in enumerate(["ok", "blocked", "cache_hit", "error", "cancelled"]):
        t = AgentTrace(f"s{i}", i, None, f"q{i}")
        if outcome == "error":
            t.flush(outcome, error_type="ValueError")
        else:
            t.flush(outcome)
    rows = _read_lines(Path(settings.AGENT_TRACE_DIR))
    assert {r["outcome"] for r in rows} == {"ok", "blocked", "cache_hit", "error", "cancelled"}
    err = next(r for r in rows if r["outcome"] == "error")
    assert err["error_type"] == "ValueError"


def test_mark_intent_none_safe():
    """mark_intent(None) 不抛不写。"""
    t = AgentTrace("s", 1, None, "q")
    t.mark_intent(None)
    t.flush("ok")
    r = _read_lines(Path(settings.AGENT_TRACE_DIR))[0]
    assert "intent" not in r
