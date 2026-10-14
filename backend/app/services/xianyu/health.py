"""
XianyuHealth — 健康检查 + 失败告警（V16.1.6）

职责：
1. 聚合 watcher / debounce / scheduler / client 状态
2. 失败计数（连续错误 N 次 → 告警）
3. 配额监控（每日发送数 / 上限）
4. 持久化（to_dict / from_dict 用于重启续跑）
5. 暴露给 FastAPI /admin/xianyu/health 端点

调用方：
  - main 启动时 → 创建 XianyuHealth 实例
  - FastAPI /admin/xianyu/health 端点 → 调 check_all()
  - ReplyScheduler 失败时 → 调 record_error()
  - 健康 OK 时 → 调 clear_errors()
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class XianyuHealth:
    """闲鱼自动化健康检查 + 告警

    设计取舍（V16.1.6）：
    - 单实例（main 启动时建一个）
    - 失败计数内存态（重启清零；后续可接 Redis 持久化）
    - 告警阈值默认 3（连续 3 错触发）
    - 暴露 to_dict / from_dict 便于跨进程同步
    """

    def __init__(
        self,
        watcher=None,
        debounce=None,
        scheduler=None,
        client=None,
        daily_quota: int = 50,
        alert_threshold: int = 3,
    ):
        self._watcher = watcher
        self._debounce = debounce
        self._scheduler = scheduler
        self._client = client
        self._daily_quota = daily_quota
        self._alert_threshold = alert_threshold
        self.errors_count: int = 0
        self.last_error: str = ""

    # =============================================================
    # 健康检查
    # =============================================================
    def check_all(self) -> dict:
        """聚合检查所有子模块状态"""
        issues: list[str] = []
        # 1. watcher
        watcher_alive = False
        if self._watcher is not None:
            try:
                watcher_alive = bool(self._watcher.is_alive())
            except Exception as e:
                logger.warning(f"health check watcher 异常: {e}")
        if not watcher_alive:
            issues.append("watcher_down")
        # 2. cookie
        cookie_valid = False
        if self._client is not None:
            try:
                cookie_valid = bool(self._client.is_enabled())
            except Exception as e:
                logger.warning(f"health check client 异常: {e}")
        if not cookie_valid:
            issues.append("cookie_invalid")
        # 3. debounce pending
        pending_count = 0
        if self._debounce is not None:
            try:
                pending_count = int(self._debounce.pending_count())
            except Exception as e:
                logger.warning(f"health check debounce 异常: {e}")
        # 4. sent_today
        sent_today = 0
        if self._scheduler is not None:
            try:
                sent_today = int(self._scheduler.sent_count())
            except Exception as e:
                logger.warning(f"health check scheduler 异常: {e}")
        remaining = max(0, self._daily_quota - sent_today)
        usage_pct = (
            round(sent_today / self._daily_quota * 100, 1)
            if self._daily_quota > 0
            else 0.0
        )
        if remaining == 0:
            issues.append("daily_quota_exhausted")
        return {
            "watcher_alive": watcher_alive,
            "cookie_valid": cookie_valid,
            "debounce_pending_count": pending_count,
            "sent_today": sent_today,
            "daily_quota": self._daily_quota,
            "daily_quota_remaining": remaining,
            "daily_quota_usage_pct": usage_pct,
            "last_error": self.last_error,
            "errors_count": self.errors_count,
            "should_alert": self.should_alert(),
            "issues": issues,
            "checked_at": time.time(),
        }

    # =============================================================
    # 失败计数 + 告警
    # =============================================================
    def record_error(self, msg: str) -> None:
        """记录一次失败"""
        self.errors_count += 1
        self.last_error = msg
        logger.warning(
            f"XianyuHealth 错误 {self.errors_count}/{self._alert_threshold}: {msg}"
        )

    def clear_errors(self) -> None:
        """清零错误（成功后调用）"""
        if self.errors_count > 0:
            logger.info(
                f"XianyuHealth 错误清零 (was {self.errors_count})"
            )
        self.errors_count = 0
        self.last_error = ""

    def should_alert(self) -> bool:
        """是否应触发告警（连续 errors_count >= threshold）"""
        return self.errors_count >= self._alert_threshold

    # =============================================================
    # 持久化
    # =============================================================
    def to_dict(self) -> dict:
        return {
            "errors_count": self.errors_count,
            "last_error": self.last_error,
        }

    def from_dict(self, d: dict) -> None:
        self.errors_count = int(d.get("errors_count", 0))
        self.last_error = str(d.get("last_error", ""))


__all__ = ["XianyuHealth"]