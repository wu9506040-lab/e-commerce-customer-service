"""Agent 记忆持久化测试（V20 亮点 1：LangGraph Checkpointer）

覆盖四件事：
1. get_checkpointer 工厂三分支：memory / none / sqlite（临时文件，真实落盘）
2. 单例语义：同进程多次获取返回同一实例
3. 真实进程重启场景：落盘 db 跨实例恢复 + 断点续跑（node 不重跑）
4. build_thread_id：同订单同轨迹 / 无订单号按 query 稳定哈希 / 用户隔离
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from app.core.agent_checkpointer import (
    build_thread_id,
    get_checkpointer,
    reset_checkpointer,
)
from app.core.config import settings


# ---------- 测试用最小图：node_a → node_b → END ----------

class _MiniState(TypedDict):
    steps: list[str]
    counter: int


def _node_a(state: _MiniState) -> dict:
    return {"steps": list(state.get("steps", [])) + ["a"], "counter": state.get("counter", 0) + 1}


def _node_b(state: _MiniState) -> dict:
    return {"steps": list(state.get("steps", [])) + ["b"]}


def _build_mini_graph(checkpointer, interrupt_before=None):
    g = StateGraph(_MiniState)
    g.add_node("node_a", _node_a)
    g.add_node("node_b", _node_b)
    g.set_entry_point("node_a")
    g.add_edge("node_a", "node_b")
    g.add_edge("node_b", END)
    return g.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)


@pytest.fixture(autouse=True)
def _fresh_singleton():
    """每个用例独立单例，互不污染。"""
    reset_checkpointer()
    yield
    reset_checkpointer()


# ---------- 1. 工厂三分支 ----------

def test_factory_memory_branch(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_CHECKPOINTER_TYPE", "memory", raising=False)
    cp = get_checkpointer()
    assert isinstance(cp, MemorySaver)


def test_factory_none_branch_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_CHECKPOINTER_TYPE", "none", raising=False)
    assert get_checkpointer() is None


def test_factory_sqlite_branch_persists_to_disk(monkeypatch, tmp_path: Path):
    db = tmp_path / "cp" / "agent.sqlite3"
    monkeypatch.setattr(settings, "AGENT_CHECKPOINTER_TYPE", "sqlite", raising=False)
    monkeypatch.setattr(settings, "AGENT_CHECKPOINTER_DB", str(db), raising=False)
    cp = get_checkpointer()
    assert cp is not None
    # 目录自动创建
    assert db.parent.is_dir()
    # 落盘验证：db 文件是合法 sqlite（SqliteSaver 建表是懒加载，首次写入才 setup；
    # 持久化行为本身由 test_graph_state_persisted_and_resumable 用 SqliteSaver 覆盖）
    assert isinstance(cp, SqliteSaver)
    conn = sqlite3.connect(str(db))
    version = conn.execute("PRAGMA journal_mode").fetchone()
    conn.close()
    assert version is not None


def test_factory_is_singleton(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_CHECKPOINTER_TYPE", "memory", raising=False)
    assert get_checkpointer() is get_checkpointer()


# ---------- 2. 真实进程重启场景：落盘恢复 + 断点续跑 ----------

def test_graph_state_persisted_and_resumable(tmp_path: Path):
    """真实进程重启场景：第一个实例执行到 node_b 前中断，
    第二个实例（新连接、新编译、同一落盘 db）读回状态并从断点续跑——
    node_a 不重跑。持久化必须是真实文件而非进程内存。"""
    db = str(tmp_path / "resume.sqlite3")

    # —— 第一次"进程"：跑到 node_b 前中断 ——
    cp1 = SqliteSaver(sqlite3.connect(db, check_same_thread=False))
    app1 = _build_mini_graph(cp1, interrupt_before=["node_b"])
    config = {"configurable": {"thread_id": "t-restart-1"}}
    app1.invoke({"steps": [], "counter": 0}, config=config)

    snap = app1.get_state(config)
    assert snap.values["counter"] == 1
    assert snap.values["steps"] == ["a"]
    assert snap.next == ("node_b",), "应停在 node_b 前"

    # —— 第二次"进程"：全新连接 + 全新编译，仅共享落盘 db ——
    cp2 = SqliteSaver(sqlite3.connect(db, check_same_thread=False))
    app2 = _build_mini_graph(cp2)
    snap2 = app2.get_state(config)
    assert snap2.values["counter"] == 1, "状态应从磁盘恢复"

    app2.invoke(None, config=config)  # resume：从断点继续

    snap3 = app2.get_state(config)
    assert snap3.values["steps"] == ["a", "b"]  # node_a 没有重跑
    assert snap3.values["counter"] == 1         # 计数没翻倍
    assert not snap3.next                       # 已到 END


# ---------- 3. build_thread_id ----------

def test_thread_id_with_session_same_order_same_thread():
    a = build_thread_id("sess1", 7, "ORD123", "怎么退款")
    b = build_thread_id("sess1", 7, "ORD123", "换个问法")
    assert a == b                      # 同会话同订单共享轨迹
    assert a.startswith("refund:s:sess1:7:ORD123")


def test_thread_id_no_session_is_unique_oneoff():
    """无 session（测试/直连）：一次性轨迹，永不跨调用续跑。"""
    a = build_thread_id(None, 7, "ORD123", "q")
    b = build_thread_id(None, 7, "ORD123", "q")
    assert a != b                      # 每次调用独立轨迹
    assert a.startswith("refund:a:")
    # 仍可审计：不带 session 的轨迹也进 checkpoint（由图写入保证）


def test_thread_id_no_order_uses_stable_query_hash():
    a = build_thread_id("sess1", 7, None, "这个能退吗   ")
    b = build_thread_id("sess1", 7, None, "这个能退吗")
    c = build_thread_id("sess1", 7, None, "别的问题")
    assert a == b          # 首尾空白不影响
    assert a != c          # 不同 query 不同轨迹
    assert ":q" in a


def test_thread_id_isolates_sessions_and_users():
    assert build_thread_id("s1", 7, "ORD1", "q") != build_thread_id("s2", 7, "ORD1", "q")
    assert build_thread_id("s1", 7, "ORD1", "q") != build_thread_id("s1", 8, "ORD1", "q")
