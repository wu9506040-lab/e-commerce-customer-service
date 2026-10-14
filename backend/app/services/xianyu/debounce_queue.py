"""
DebounceQueue — 30s 静默合并（V16.1）

职责：
1. 收到新消息 → 入队（按 cid 分组）
2. 同 cid 30s 静默期内的新消息合并（重置 timer）
3. 静默结束（无新消息）→ 调 fire_callback 推下游
4. 多 cid 各自独立 timer
5. shutdown() 优雅关闭（fire 所有 pending）

调用方：
  XianyuWatcher → on_message(event)
    → DebounceQueue（30s 合并）
    → ReplyScheduler.on_fire(cid, messages)
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class DebounceQueue:
    """30s 静默合并队列

    设计取舍（V16.1）：
    - 单进程（不跨进程；Redis 持久化在 ReplyScheduler 层做）
    - threading.Timer（轻量；不引入 APScheduler / Celery 等重组件）
    - fire_callback 同步调用（业务方负责 thread-safety）
    - 异常隔离：一个 cid fire 失败不阻断其他 cid
    """

    def __init__(
        self,
        fire_callback: Callable[[str, list[dict]], None],
        window_seconds: float = 30.0,
    ):
        self._fire_cb = fire_callback
        self._window = window_seconds
        # cid → {messages, timer}
        self._buckets: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._closed = False

    def on_message(self, event: dict) -> None:
        """收到新消息（XianyuWatcher 调）

        行为：
        - 已存在 cid → 追加消息 + 重置 timer
        - 不存在 cid → 创建 bucket + 起 timer
        - 已 shutdown → no-op
        """
        cid = event.get("cid", "")
        if not cid:
            logger.debug(f"DebounceQueue 跳过无 cid 事件: {event}")
            return
        if self._closed:
            logger.warning(f"DebounceQueue 已关闭，丢弃消息 cid={cid}")
            return
        with self._lock:
            bucket = self._buckets.get(cid)
            if bucket is None:
                bucket = {"messages": [], "timer": None}
                self._buckets[cid] = bucket
            bucket["messages"].append(event)
            # 重置 timer
            old_timer = bucket["timer"]
            if old_timer is not None:
                old_timer.cancel()
            timer = threading.Timer(
                self._window, self._on_timer_fire, args=(cid,)
            )
            timer.daemon = True
            bucket["timer"] = timer
            timer.start()
        logger.debug(
            f"DebounceQueue on_message cid={cid} "
            f"msgs_now={len(bucket['messages'])}"
        )

    def _on_timer_fire(self, cid: str) -> None:
        """timer fire → 取消息 + 调 callback + 清 bucket"""
        with self._lock:
            bucket = self._buckets.pop(cid, None)
        if bucket is None:
            return
        messages = bucket["messages"]
        if not messages:
            return
        try:
            self._fire_cb(cid, list(messages))
        except Exception as e:
            # 异常隔离：不影响其他 cid
            logger.exception(
                f"DebounceQueue fire_callback 异常 cid={cid}: "
                f"{type(e).__name__}: {e}"
            )

    # =============================================================
    # 状态查询
    # =============================================================
    def pending_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def is_pending(self, cid: str) -> bool:
        with self._lock:
            return cid in self._buckets

    def get_pending_messages(self, cid: str) -> list[dict]:
        with self._lock:
            bucket = self._buckets.get(cid)
            if bucket is None:
                return []
            return list(bucket["messages"])

    # =============================================================
    # 优雅关闭
    # =============================================================
    def shutdown(self) -> None:
        """立即 fire 所有 pending（不丢消息），关闭队列"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # 取消所有 timer + 同步 fire
            buckets = list(self._buckets.items())
            self._buckets.clear()
        for cid, bucket in buckets:
            timer = bucket.get("timer")
            if timer is not None:
                timer.cancel()
            messages = bucket.get("messages") or []
            if messages:
                try:
                    self._fire_cb(cid, list(messages))
                except Exception as e:
                    logger.exception(
                        f"DebounceQueue shutdown fire 异常 cid={cid}: "
                        f"{type(e).__name__}: {e}"
                    )
        logger.info("DebounceQueue shutdown 完成")


__all__ = ["DebounceQueue"]