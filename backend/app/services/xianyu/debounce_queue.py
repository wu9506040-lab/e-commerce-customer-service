"""
DebounceQueue — V16.3.1 首次 5s / 后续 3s 静默合并
           V16.4 标点检测：句末标点结尾 → 提前触发（话已说完启发式）

职责：
1. 收到新消息 → 入队（按 cid 分组）
2. 同 cid 30s 静默期内的新消息合并（重置 timer）
3. 静默结束（无新消息）→ 调 fire_callback 推下游
4. 多 cid 各自独立 timer
5. shutdown() 优雅关闭（fire 所有 pending）
6. V16.4 启发式：消息末尾是否句末标点 ?。！？ → 立即触发短 window

调用方：
  XianyuWatcher → on_message(event)
    → DebounceQueue（启发式标点 + 静默合并）
    → ReplyScheduler.on_fire(cid, messages)
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# V16.4 启发式：句末标点集合（中文 + 英文）
_TERMINAL_PUNCT = frozenset("?？!！.。")


def _ends_with_terminal_punct(text: str) -> bool:
    """检测文本结尾是否为句末标点（表示一句话已说完）

    Returns True if 末尾是 ?？!！.。
    """
    if not text:
        return False
    text = text.rstrip()
    if not text:
        return False
    return text[-1] in _TERMINAL_PUNCT


class DebounceQueue:
    """30s 静默合并队列（V16.4 加启发式标点检测）

    设计取舍（V16.1）：
    - 单进程（不跨进程；Redis 持久化在 ReplyScheduler 层做）
    - threading.Timer（轻量；不引入 APScheduler / Celery 等重组件）
    - fire_callback 同步调用（业务方负责 thread-safety）
    - 异常隔离：一个 cid fire 失败不阻断其他 cid

    V16.4 启发式断句：
    - 末尾是句末标点 (?？!！.。) → 话已说完 → 用 _short_window 快速 fire
    - 否则 → 走 _first/_next window 继续等（话可能还没说完）
    - 防止打断买家打字（这是用户最在意的体验）
    """

    def __init__(
        self,
        fire_callback: Callable[[str, list[dict]], None],
        window_seconds: float = 30.0,
    ):
        self._fire_cb = fire_callback
        # V16.3.1: 首次 5s / 后续 3s（按 BuyerProfile 缓存判断）
        self._window = window_seconds
        # V16.4 fix: window_seconds 作兜底默认,env 覆盖；不破坏老测试（window_seconds=0.1 加速）
        self._first_window = float(os.environ.get("XIANYU_DEBOUNCE_FIRST", str(window_seconds)))
        self._next_window = float(os.environ.get("XIANYU_DEBOUNCE_NEXT", str(window_seconds)))
        # V16.4: 句末标点结尾时的短 window（话已说完快速触发）
        self._short_window = float(os.environ.get("XIANYU_DEBOUNCE_SHORT", str(min(window_seconds, 1.5))))
        # V16.5: 默认值由 5s/3s 收紧到 3s/3s/1.5s,配合 daemon 让 on_fire 后 27s 阻塞问题（mysql DNS 失败）一并修复
        # 记录每个 cid 是否首次 fire
        self._first_fire_done: set[str] = set()
        # cid → {messages, timer}
        self._buckets: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._closed = False

    def on_message(self, event: dict) -> None:
        """收到新消息（XianyuWatcher 调）

        行为：
        - 已存在 cid → 追加消息 + 重置 timer
        - 不存在 cid → 创建 bucket + 起 timer
        - V16.4 启发式：消息末尾是句末标点 → 用 _short_window（提前触发）
        - 否则 → 用 _first_window / _next_window（继续等）
        - 已 shutdown → no-op
        """
        cid = event.get("cid", "")
        if not cid:
            logger.debug(f"DebounceQueue 跳过无 cid 事件: {event}")
            return
        if self._closed:
            logger.warning(f"DebounceQueue 已关闭，丢弃消息 cid={cid}")
            return

        # V16.4: 启发式 — 用当前消息的 text 末尾判定（不依赖 bucket 已有消息）
        current_text = str(event.get("text", "") or "")
        punct_terminal = _ends_with_terminal_punct(current_text)

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
            # V16.4 启发式选 window:
            # - 末尾是句末标点 → 短 window（话已说完）
            # - 否则 → 首次 5s / 后续 3s（话还没完）
            if punct_terminal:
                win = self._short_window
            else:
                is_first = cid not in self._first_fire_done
                win = self._first_window if is_first else self._next_window
            timer = threading.Timer(win, self._on_timer_fire, args=(cid,))
            timer.daemon = True
            bucket["timer"] = timer
            timer.start()
        logger.debug(
            f"DebounceQueue on_message cid={cid} "
            f"msgs_now={len(bucket['messages'])} "
            f"punct_terminal={punct_terminal} win={win:.2f}s"
        )

    def _on_timer_fire(self, cid: str) -> None:
        """timer fire → 取消息 + 调 callback + 清 bucket"""
        with self._lock:
            # 标记为"首次 fire 完成"（下次切换到 next_window）
            self._first_fire_done.add(cid)
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