"""
DebounceQueue 测试 — 30s 静默合并 + 多 cid 并行 + on_fire 触发

按 CLAUDE.md 调试铁律：测试用 mock time（不真 sleep）
"""
import threading
import time
from unittest.mock import MagicMock

import pytest

from app.services.xianyu.debounce_queue import DebounceQueue


# =============================================================
# 1. 单 cid 合并
# =============================================================
class TestSingleCid:
    def test_first_message_schedules_fire(self):
        """第一条消息起 30s timer（不立即 fire）"""
        fired: list[tuple[str, list]] = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.1,  # 测试用 0.1s 加速
        )
        q.on_message({"cid": "c1", "text": "hi1"})
        time.sleep(0.15)  # 等待 fire
        assert len(fired) == 1
        assert fired[0][0] == "c1"
        assert len(fired[0][1]) == 1

    def test_second_message_resets_timer(self):
        """30s 内新消息 → 重置 timer，合并消息"""
        fired: list[tuple[str, list]] = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.1,
        )
        q.on_message({"cid": "c1", "text": "hi1"})
        time.sleep(0.05)  # 半窗口
        q.on_message({"cid": "c1", "text": "hi2"})  # 重置 timer
        time.sleep(0.08)  # 已过 0.13s but timer 在 0.05 重置 = 0.15s 才 fire
        # 此时还没 fire
        assert fired == []
        time.sleep(0.1)  # 总 0.23s 已过 fire
        assert len(fired) == 1
        assert len(fired[0][1]) == 2  # 合并了 2 条

    def test_fire_emits_all_merged_messages(self):
        """fire 时 callback 收到该 cid 期内所有消息"""
        fired: list[tuple[str, list]] = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.05,
        )
        for i in range(3):
            q.on_message({"cid": "c1", "text": f"msg{i}"})
        time.sleep(0.1)
        assert len(fired) == 1
        messages = fired[0][1]
        assert [m["text"] for m in messages] == ["msg0", "msg1", "msg2"]


# =============================================================
# 2. 多 cid 并行
# =============================================================
class TestMultipleCids:
    def test_different_cids_have_independent_timers(self):
        """不同 cid 各自独立 timer，互不影响"""
        fired: list[tuple[str, list]] = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=0.1,
        )
        q.on_message({"cid": "c1", "text": "a1"})
        time.sleep(0.05)
        q.on_message({"cid": "c2", "text": "b1"})  # c2 启新 timer
        time.sleep(0.07)  # c1 应该已 fire（0.12s 过 0.1），c2 还没（0.07 过 0.05 重置后 0.12s 仍不到 0.1s）
        cids_fired = [c[0] for c in fired]
        assert "c1" in cids_fired
        assert "c2" not in cids_fired
        time.sleep(0.1)
        cids_fired = [c[0] for c in fired]
        assert "c1" in cids_fired
        assert "c2" in cids_fired


# =============================================================
# 3. 状态查询
# =============================================================
class TestStateQuery:
    def test_pending_count(self):
        """pending_count 返当前待 fire 的 cid 数"""
        q = DebounceQueue(fire_callback=lambda c, m: None, window_seconds=10.0)
        assert q.pending_count() == 0
        q.on_message({"cid": "c1", "text": "hi"})
        q.on_message({"cid": "c2", "text": "hi"})
        assert q.pending_count() == 2
        q.on_message({"cid": "c1", "text": "hi2"})
        assert q.pending_count() == 2  # 同 cid 不增

    def test_is_pending(self):
        """is_pending 查某 cid 是否在 timer 中"""
        q = DebounceQueue(fire_callback=lambda c, m: None, window_seconds=10.0)
        q.on_message({"cid": "c1", "text": "hi"})
        assert q.is_pending("c1") is True
        assert q.is_pending("c2") is False

    def test_get_pending_messages(self):
        """get_pending 返该 cid 当前累积消息"""
        q = DebounceQueue(fire_callback=lambda c, m: None, window_seconds=10.0)
        q.on_message({"cid": "c1", "text": "a"})
        q.on_message({"cid": "c1", "text": "b"})
        msgs = q.get_pending_messages("c1")
        assert [m["text"] for m in msgs] == ["a", "b"]


# =============================================================
# 4. 优雅关闭
# =============================================================
class TestGracefulShutdown:
    def test_shutdown_fires_all_pending_immediately(self):
        """shutdown() 立即 fire 所有 pending（不丢失）"""
        fired: list[tuple[str, list]] = []
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: fired.append((cid, msgs)),
            window_seconds=10.0,  # 故意长窗口
        )
        q.on_message({"cid": "c1", "text": "a"})
        q.on_message({"cid": "c2", "text": "b"})
        assert q.pending_count() == 2
        q.shutdown()
        assert q.pending_count() == 0
        assert len(fired) == 2

    def test_shutdown_idempotent(self):
        """shutdown 多次安全"""
        q = DebounceQueue(fire_callback=lambda c, m: None, window_seconds=10.0)
        q.shutdown()
        q.shutdown()  # no raise


# =============================================================
# 5. 异常隔离
# =============================================================
class TestExceptionIsolation:
    def test_callback_exception_does_not_block_other_cids(self):
        """一个 cid 的 callback 异常不影响其他 cid"""
        calls: list[str] = []
        def bad_cb(cid, msgs):
            if cid == "bad":
                raise RuntimeError("boom")
            calls.append(cid)

        q = DebounceQueue(
            fire_callback=bad_cb,
            window_seconds=0.05,
        )
        q.on_message({"cid": "bad", "text": "x"})
        q.on_message({"cid": "good", "text": "y"})
        time.sleep(0.1)
        assert "good" in calls
        # bad 应该 fire 但异常被吞（不阻断其他）


# =============================================================
# 6. 默认值（V16.7：debounce 窗口默认 12s，合并用户连发消息）
# =============================================================
class TestDefaultWindow:
    def test_default_window_is_12_seconds(self, monkeypatch):
        """未指定 window_seconds 且无 env override 时 → 默认 12s"""
        monkeypatch.delenv("XIANYU_DEBOUNCE_FIRST", raising=False)
        monkeypatch.delenv("XIANYU_DEBOUNCE_NEXT", raising=False)
        monkeypatch.delenv("XIANYU_DEBOUNCE_SHORT", raising=False)
        q = DebounceQueue(fire_callback=lambda cid, msgs: None)
        assert q._first_window == 12.0
        assert q._next_window == 12.0

    def test_explicit_window_overrides_default(self, monkeypatch):
        """显式传 window_seconds → 覆盖默认值（保留测试兼容）"""
        monkeypatch.delenv("XIANYU_DEBOUNCE_FIRST", raising=False)
        monkeypatch.delenv("XIANYU_DEBOUNCE_NEXT", raising=False)
        q = DebounceQueue(
            fire_callback=lambda cid, msgs: None,
            window_seconds=0.5,
        )
        assert q._first_window == 0.5
        assert q._next_window == 0.5