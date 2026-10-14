"""
XianyuHealth 测试 — 健康检查 + 失败告警

按 CLAUDE.md 调试铁律：测试 mock watcher / debounce / scheduler
"""
import time
from unittest.mock import MagicMock

import pytest

from app.services.xianyu.health import XianyuHealth


# =============================================================
# 1. 健康检查
# =============================================================
class TestHealthCheck:
    def test_check_all_aggregates_state(self):
        """check_all() 聚合所有子模块状态"""
        # mock watcher
        mock_watcher = MagicMock()
        mock_watcher.is_alive.return_value = True
        # mock debounce
        mock_debounce = MagicMock()
        mock_debounce.pending_count.return_value = 3
        # mock scheduler
        mock_scheduler = MagicMock()
        mock_scheduler.sent_count.return_value = 12
        # mock client
        mock_client = MagicMock()
        mock_client.is_enabled.return_value = True
        mock_client.health_check.return_value = {
            "enabled": True, "mcp_sdk_available": True, "cookie_exists": True
        }

        h = XianyuHealth(
            watcher=mock_watcher,
            debounce=mock_debounce,
            scheduler=mock_scheduler,
            client=mock_client,
        )
        status = h.check_all()
        assert status["watcher_alive"] is True
        assert status["cookie_valid"] is True
        assert status["debounce_pending_count"] == 3
        assert status["sent_today"] == 12
        assert "last_error" in status
        assert "errors_count" in status

    def test_check_all_watcher_down(self):
        """watcher 死了 → watcher_alive=False"""
        mock_watcher = MagicMock()
        mock_watcher.is_alive.return_value = False
        h = XianyuHealth(
            watcher=mock_watcher,
            debounce=MagicMock(),
            scheduler=MagicMock(),
            client=MagicMock(),
        )
        status = h.check_all()
        assert status["watcher_alive"] is False
        assert "watcher_down" in status.get("issues", [])

    def test_check_all_cookie_invalid(self):
        """cookie 失效 → cookie_valid=False"""
        mock_client = MagicMock()
        mock_client.is_enabled.return_value = False
        h = XianyuHealth(
            watcher=MagicMock(is_alive=MagicMock(return_value=True)),
            debounce=MagicMock(),
            scheduler=MagicMock(),
            client=mock_client,
        )
        status = h.check_all()
        assert status["cookie_valid"] is False


# =============================================================
# 2. 失败计数 + 告警
# =============================================================
class TestErrorCounting:
    def test_record_error_increments(self):
        """record_error 累加"""
        h = XianyuHealth(
            watcher=MagicMock(), debounce=MagicMock(),
            scheduler=MagicMock(), client=MagicMock()
        )
        h.record_error("mcp_call_timeout")
        h.record_error("send_message_failed")
        assert h.errors_count == 2
        assert h.last_error == "send_message_failed"

    def test_clear_errors_resets(self):
        """clear_errors 重置计数"""
        h = XianyuHealth(
            watcher=MagicMock(), debounce=MagicMock(),
            scheduler=MagicMock(), client=MagicMock()
        )
        h.record_error("err1")
        h.record_error("err2")
        h.clear_errors()
        assert h.errors_count == 0
        assert h.last_error == ""

    def test_should_alert_after_3_errors(self):
        """连续 3 错 → should_alert=True"""
        h = XianyuHealth(
            watcher=MagicMock(), debounce=MagicMock(),
            scheduler=MagicMock(), client=MagicMock(),
            alert_threshold=3,
        )
        assert h.should_alert() is False
        h.record_error("e1")
        h.record_error("e2")
        assert h.should_alert() is False
        h.record_error("e3")
        assert h.should_alert() is True
        # clear 后恢复
        h.clear_errors()
        assert h.should_alert() is False


# =============================================================
# 3. 持久化（重启后还能读 last_error）
# =============================================================
class TestPersistence:
    def test_to_dict_from_dict(self):
        """to_dict + from_dict 持久化往返"""
        h1 = XianyuHealth(
            watcher=MagicMock(), debounce=MagicMock(),
            scheduler=MagicMock(), client=MagicMock(),
        )
        h1.record_error("test_err")
        d = h1.to_dict()
        h2 = XianyuHealth(
            watcher=MagicMock(), debounce=MagicMock(),
            scheduler=MagicMock(), client=MagicMock(),
        )
        h2.from_dict(d)
        assert h2.last_error == "test_err"
        assert h2.errors_count == 1


# =============================================================
# 4. 每日配额监控
# =============================================================
class TestQuotaMonitoring:
    def test_daily_quota_remaining(self):
        """已发 45 / 上限 50 → 剩余 5"""
        mock_scheduler = MagicMock()
        mock_scheduler.sent_count.return_value = 45
        h = XianyuHealth(
            watcher=MagicMock(), debounce=MagicMock(),
            scheduler=mock_scheduler, client=MagicMock(),
            daily_quota=50,
        )
        status = h.check_all()
        assert status["sent_today"] == 45
        assert status["daily_quota_remaining"] == 5
        assert status["daily_quota_usage_pct"] == 90.0

    def test_quota_exhausted_in_issues(self):
        """已发 50 / 上限 50 → issues 报警"""
        mock_scheduler = MagicMock()
        mock_scheduler.sent_count.return_value = 50
        h = XianyuHealth(
            watcher=MagicMock(), debounce=MagicMock(),
            scheduler=mock_scheduler, client=MagicMock(),
            daily_quota=50,
        )
        status = h.check_all()
        assert "daily_quota_exhausted" in status.get("issues", [])