"""
XianyuWatcher 测试 — 覆盖 subprocess 启动 + JSONL 解析 + 崩溃恢复 + 探活 + 优雅关闭

按 CLAUDE.md 调试铁律：测试不依赖真实 goofish（mock subprocess）
"""
import json
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.xianyu.watcher import XianyuWatcher, WatcherAuthError


# =============================================================
# 1. 探活（启动时）
# =============================================================
class TestStartupHealthCheck:
    def test_auth_status_ok(self, monkeypatch):
        """auth status 返 valid=True → start 成功"""
        monkeypatch.setattr(
            "app.services.xianyu.watcher.run_auth_status",
            lambda: {"unb": "u1", "tracknick": "t1", "valid": True},
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None  # alive
            mock_proc.stdout = MagicMock()
            mock_popen.return_value = mock_proc
            w = XianyuWatcher(callback=lambda e: None)
            w.start()
            assert w.is_alive() is True
            w.stop()

    def test_auth_status_invalid_raises(self, monkeypatch):
        """auth status 失败 → 抛 WatcherAuthError（不启动 subprocess）"""
        monkeypatch.setattr(
            "app.services.xianyu.watcher.run_auth_status",
            lambda: {"unb": "", "tracknick": "", "valid": False},
        )
        w = XianyuWatcher(callback=lambda e: None)
        with pytest.raises(WatcherAuthError):
            w.start()


# =============================================================
# 2. JSONL 解析（核心）
# =============================================================
class TestStdoutParser:
    def test_parses_event_line(self):
        """每行 JSON 解析成功 → 调 callback"""
        events: list[dict] = []
        w = XianyuWatcher(callback=lambda e: events.append(e))
        line = json.dumps(
            {
                "event": "message",
                "cid": "c1",
                "send_user_id": "u2",
                "text": "在吗",
                "timestamp": 1234567890,
            },
            ensure_ascii=False,
        )
        w._handle_line(line)
        assert len(events) == 1
        assert events[0]["cid"] == "c1"
        assert events[0]["text"] == "在吗"

    def test_skips_blank_lines(self):
        """空行 / 纯空白 → 跳过（不调 callback）"""
        events: list[dict] = []
        w = XianyuWatcher(callback=lambda e: events.append(e))
        w._handle_line("")
        w._handle_line("   ")
        assert events == []

    def test_skips_invalid_json(self):
        """坏 JSON → 跳过（不抛）"""
        events: list[dict] = []
        w = XianyuWatcher(callback=lambda e: events.append(e))
        w._handle_line("not json{")
        w._handle_line("{unclosed")
        assert events == []

    def test_skips_non_message_event(self):
        """非 message 事件（heartbeat/connected）→ 跳过"""
        events: list[dict] = []
        w = XianyuWatcher(callback=lambda e: events.append(e))
        w._handle_line(json.dumps({"event": "connected"}))
        w._handle_line(json.dumps({"event": "heartbeat"}))
        w._handle_line(json.dumps({"event": "message", "cid": "c1", "text": "hi"}))
        assert len(events) == 1
        assert events[0]["text"] == "hi"


# =============================================================
# 3. 崩溃恢复（watchdog）
# =============================================================
class TestCrashRecovery:
    def test_restart_on_subprocess_death(self, monkeypatch):
        """subprocess 死后自动重启（最多 N 次）"""
        monkeypatch.setattr(
            "app.services.xianyu.watcher.run_auth_status",
            lambda: {"unb": "u1", "valid": True},
        )
        # mock subprocess：第一次 poll() 返 0（已死），第二次 alive
        with patch("subprocess.Popen") as mock_popen:
            mock_proc1 = MagicMock()
            mock_proc1.poll.return_value = 0  # 死
            mock_proc1.stdout = MagicMock()
            mock_proc2 = MagicMock()
            mock_proc2.poll.return_value = None  # 活
            mock_proc2.stdout = MagicMock()
            mock_popen.side_effect = [mock_proc1, mock_proc2]
            w = XianyuWatcher(callback=lambda e: None)
            w.start()
            # 触发一次 watchdog
            w._watchdog_tick()
            assert mock_popen.call_count == 2  # 重启了一次
            w.stop()

    def test_max_restart_exceeded_raises(self, monkeypatch):
        """重启超 5 次 → 永久停止 + 记录 fatal log"""
        monkeypatch.setattr(
            "app.services.xianyu.watcher.run_auth_status",
            lambda: {"unb": "u1", "valid": True},
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = 0  # 总死
            mock_proc.stdout = MagicMock()
            mock_popen.return_value = mock_proc
            w = XianyuWatcher(callback=lambda e: None)
            w.start()
            # 触发 6 次 watchdog
            for _ in range(6):
                w._watchdog_tick()
            assert w.is_alive() is False
            assert w._restart_count >= 5


# =============================================================
# 4. 优雅关闭
# =============================================================
class TestGracefulShutdown:
    def test_stop_terminates_subprocess(self, monkeypatch):
        """stop() 关闭 subprocess + reader 线程"""
        monkeypatch.setattr(
            "app.services.xianyu.watcher.run_auth_status",
            lambda: {"unb": "u1", "valid": True},
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdout = MagicMock()
            mock_popen.return_value = mock_proc
            w = XianyuWatcher(callback=lambda e: None)
            w.start()
            w.stop()
            mock_proc.terminate.assert_called()
            assert w.is_alive() is False


# =============================================================
# 5. 状态
# =============================================================
class TestState:
    def test_initial_state(self):
        """未 start 时 is_alive False"""
        w = XianyuWatcher(callback=lambda e: None)
        assert w.is_alive() is False
        assert w._restart_count == 0

    def test_double_start_is_idempotent(self, monkeypatch):
        """start 两次不重复起 subprocess"""
        monkeypatch.setattr(
            "app.services.xianyu.watcher.run_auth_status",
            lambda: {"unb": "u1", "valid": True},
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdout = MagicMock()
            mock_popen.return_value = mock_proc
            w = XianyuWatcher(callback=lambda e: None)
            w.start()
            w.start()  # 第二次应 no-op
            assert mock_popen.call_count == 1
            w.stop()


# =============================================================
# 6. Stale watchdog 指数退避（V16.6 fix）
# =============================================================
class TestStaleWatchdogBackoff:
    def _make_watcher_with_stale(self, monkeypatch, max_restart=5):
        """造一个 watcher，subprocess 活、_last_message_event_at 已设置

        同时 monkeypatch _stop_event.wait 立即返回（避免 backoff 真 sleep 卡测试）
        """
        # 关键：mock _stop_event.wait 让 backoff 立即返回（不等满 30/60s）
        import app.services.xianyu.watcher as w_mod
        original_wait = w_mod.threading.Event.wait
        # 但 _stop_event 是 instance 上的属性；用 patch.object 替换 instance.wait
        # 这里用 monkeypatch.setattr 后置到 instance
        monkeypatch.setattr(
            "app.services.xianyu.watcher.run_auth_status",
            lambda: {"unb": "u1", "valid": True},
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdout = MagicMock()
            mock_popen.return_value = mock_proc
            w = XianyuWatcher(
                callback=lambda e: None,
                max_restart=max_restart,
            )
            w.start()
            # mock _stop_event.wait 立即返回（不 sleep）
            # 但仍把 timeout 参数记下来用于断言
            monkeypatch.setattr(w._stop_event, "wait",
                                lambda timeout=None: False)  # 立即返回 False = 不被打断
            # 重新包装记 timeout
            backoffs = []
            def wait_capturing(timeout=None):
                if timeout is not None and timeout > 0.5:  # backoff > 0.5s
                    backoffs.append(timeout)
                return False  # False = 不被 stop_event set
            monkeypatch.setattr(w._stop_event, "wait", wait_capturing)
            w._test_backoffs = backoffs  # 暴露给外部断言
            w._last_message_event_at = time.time() - 70.0
            return w

    def test_stale_first_restart_backoff_30s(self, monkeypatch):
        """第 1 次 stale → backoff 30s"""
        w = self._make_watcher_with_stale(monkeypatch)
        try:
            w._stale_watchdog_tick()
            assert w._restart_count == 1
            assert w._test_backoffs == [30.0]
        finally:
            w.stop()

    def test_stale_exponential_backoff_progression(self, monkeypatch):
        """stale 触发 1→2→3 次，backoff 30s/60s/120s 递增"""
        w = self._make_watcher_with_stale(monkeypatch)
        try:
            for _ in range(3):
                w._last_message_event_at = time.time() - 70.0
                w._stale_watchdog_tick()
            assert w._test_backoffs == [30.0, 60.0, 120.0]
            assert w._restart_count == 3
        finally:
            w.stop()

    def test_stale_caps_backoff_at_480s(self, monkeypatch):
        """第 5 次 stale → backoff 480s（封顶）"""
        w = self._make_watcher_with_stale(monkeypatch)
        try:
            # 直接打到第 5 次
            w._restart_count = 4  # 下一次 ++ 后 = 5
            w._last_message_event_at = time.time() - 70.0
            w._stale_watchdog_tick()
            assert w._test_backoffs == [480.0]
            assert w._restart_count == 5
        finally:
            w.stop()

    def test_stale_over_max_restart_does_not_permanently_stop(self, monkeypatch):
        """超过 max_restart=5 次 stale → 打 ERROR 但不永久停（V16.6 fix）"""
        w = self._make_watcher_with_stale(monkeypatch, max_restart=5)
        try:
            # 触发 7 次 stale（前 5 次 backoff=30/60/120/240/480，第 6/7 次 backoff=0）
            for _ in range(7):
                w._last_message_event_at = time.time() - 70.0
                w._stale_watchdog_tick()
            assert w._restart_count == 7
            assert not w._stop_event.is_set()  # V16.6: 不永久停
            # 前 5 次有 backoff，后 2 次 backoff=0 不过 wait
            assert w._test_backoffs == [30.0, 60.0, 120.0, 240.0, 480.0]
        finally:
            w.stop()

    def test_stale_over_max_restart_skips_backoff(self, monkeypatch):
        """超 max_restart 后 backoff=0（不浪费时间，wait 不被调）"""
        w = self._make_watcher_with_stale(monkeypatch, max_restart=5)
        try:
            for _ in range(8):
                w._last_message_event_at = time.time() - 70.0
                w._stale_watchdog_tick()
            # 1-5 次 backoff（5 个），6-8 次 backoff=0（_stop_event.wait 不被调）
            assert len(w._test_backoffs) == 5
            assert w._restart_count == 8
        finally:
            w.stop()

    def test_stale_backoff_can_be_interrupted_by_stop(self, monkeypatch):
        """backoff sleep 可被 stop 打断（_stop_event.wait 返 True → return）"""
        monkeypatch.setattr(
            "app.services.xianyu.watcher.run_auth_status",
            lambda: {"unb": "u1", "valid": True},
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdout = MagicMock()
            mock_popen.return_value = mock_proc
            w = XianyuWatcher(callback=lambda e: None, max_restart=5)
            w.start()
            w._last_message_event_at = time.time() - 70.0
            # mock _stop_event.wait 返 True（被打断）
            w._stop_event.set()  # 模拟 stop
            spawn_count_before = mock_popen.call_count
            w._stale_watchdog_tick()
            # wait 返 True → 函数 return，不 spawn
            assert mock_popen.call_count == spawn_count_before
            w._stop_event.clear()  # 清理