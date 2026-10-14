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