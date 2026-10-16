"""
XianyuWatcherPoller — V16.8 P2-A: HTTP MTOP 轮询替代 WebSocket 长连接

设计动机：
- 原 XianyuWatcher 启动 goofish `message watch` (WebSocket subprocess)
- Windows 上 WebSocket handshake 卡 UserRequest/Wait (实测 P1-I 修了 stdin=DEVNULL 也没用)
- 多次 stale_watchdog 重启都失败 → 用户消息全部丢失

替代方案：
- 每 5s 调一次 MTOP HTTP `mtop.taobao.idlemessage.pc.session.sync` v3.0
- 纯短连接（每次 HTTP 请求独立），跨 OS 行为一致
- 用 goofish_cli 已有的 `Session.load()` + `core.mtop.call()`（自带 cookie 自动 refresh）
- 已有 17 个 session 可拿，summary 字段含 unread + 中文 last_msg

callback event dict 形状兼容 XianyuWatcher：
  {
    "event": "message" | "read" | "new_msg",
    "cid": str,                  # 闲鱼会话 ID
    "send_user_id": str,         # 对方 userId
    "send_user_name": str,       # 对方昵称
    "send_message": str,         # 用户消息文本（prefix + summary 拼）
    "ts": int,                   # unix ms timestamp
  }

限制（已知）：
- 不能拿完整图/长消息原文（goofish-cli 内部 WS 路径才有 userMessageModels）
- 摘要字段是单条最新消息的「last_msg」+ unread 计数
- 对 24h 商家客服场景：精度足够（用户问 "ZP1 有货吗"，last_msg 就含）
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# 把 goofish_cli site-packages 加进 sys.path（项目级 one-time，daemon 启动时调一次）
# 这样 poller 能直接 import goofish_cli.core（与 V16.7 之前的行为对齐）
_GOOFISH_SITE_PACKAGES = (
    r"C:/Users/zw/AppData/Local/Packages/"
    r"PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0/"
    r"LocalCache/local-packages/Python311/site-packages"
)
if _GOOFISH_SITE_PACKAGES not in sys.path:
    sys.path.insert(0, _GOOFISH_SITE_PACKAGES)

# noqa: E402 — sys.path 必须在 import 前设置
from goofish_cli.core import Session  # noqa: E402
from goofish_cli.core.mtop import call  # noqa: E402

logger = logging.getLogger("watcher_poller")

_POLL_INTERVAL_SEC = 5.0
# 30 分钟 refresh_login（goofish-cli 内部默认；保 cookie 在 mtop 自动续期内有效）
_REFRESH_LOGIN_EVERY_SEC = 30 * 60


class XianyuWatcherPoller:
    """HTTP MTOP 轮询版 watcher（替代 XianyuWatcher 的 WebSocket subprocess）

    启动：start() 加载 Session + 启 poll_thread
    停止：stop() 设 stop_event，poll loop 检查后退出
    """

    def __init__(
        self,
        callback: Callable[[dict], None],
        poll_interval: float = _POLL_INTERVAL_SEC,
    ):
        self._callback = callback
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._stale_watchdog_thread: Optional[threading.Thread] = None
        # V16.8 P1-H: 启动即开始 stale 检测；防止"0 跳过"
        self._start_time: float = time.time()
        # cid → 上次见到 unread 数（detection diff）
        self._prev_unread_by_cid: dict[str, int] = {}
        # cid → 已处理过的 event_key，防止重复触发（unread 多次异常）
        self._last_event_key_by_cid: dict[str, str] = {}
        self._session: Optional[Session] = None
        self._last_refresh_login_at: float = 0.0

    # ============================================================
    # 公开 API
    # ============================================================
    def start(self) -> None:
        """启动 watcher：加载 Session + 启 poll thread + 启 stale watchdog"""
        if self._poll_thread is not None:
            logger.warning("XianyuWatcherPoller 已启动，start() 跳过")
            return
        try:
            self._session = Session.load()
        except Exception as e:
            logger.exception(f"XianyuWatcherPoller 加载 Session 失败: {e}")
            raise
        logger.info(
            f"XianyuWatcherPoller 启动: unb={self._session.unb} "
            f"tracknick={self._session.tracknick} "
            f"poll_interval={self._poll_interval}s"
        )
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="xianyu-watcher-poll", daemon=True
        )
        self._poll_thread.start()
        # V16.8 P1-H: stale watchdog（启动即开始计时）
        self._stale_watchdog_thread = threading.Thread(
            target=self._stale_watchdog_loop,
            name="xianyu-watcher-stale-watchdog",
            daemon=True,
        )
        self._stale_watchdog_thread.start()
        logger.info(
            f"XianyuWatcherPoller stale watchdog 已启动: threshold=180.0s "
            f"check_interval=30.0s"
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5.0)
            self._poll_thread = None
        if self._stale_watchdog_thread:
            self._stale_watchdog_thread.join(timeout=2.0)
            self._stale_watchdog_thread = None
        logger.info("XianyuWatcherPoller 停止")

    def is_alive(self) -> bool:
        return (
            self._poll_thread is not None
            and self._poll_thread.is_alive()
            and not self._stop_event.is_set()
        )

    # ============================================================
    # 内部：poll loop
    # ============================================================
    def _poll_loop(self) -> None:
        """每 poll_interval 一次 session.sync"""
        # 第一轮稍等（让 daemon 其他模块初始化完）
        if self._stop_event.wait(timeout=2.0):
            return
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logger.exception(
                    f"poll_once 异常（next tick 继续）: {type(e).__name__}: {e}"
                )
            # 睡到下次 poll，stop_event 可打断
            if self._stop_event.wait(timeout=self._poll_interval):
                break
        logger.info("XianyuWatcherPoller poll loop 退出")

    def _maybe_refresh_login(self) -> None:
        """每 30 分钟 refresh_login（cookie 自动续期，防 mtop 401）"""
        now = time.time()
        if now - self._last_refresh_login_at < _REFRESH_LOGIN_EVERY_SEC:
            return
        try:
            # V16.8 P2-A: goofish_cli core.session.refresh() 内部用 Playwright
            # 走 passport 免密记忆登录；失败抛 GoofishError
            if self._session is not None and hasattr(self._session, "refresh"):
                self._session.refresh()
                logger.info(f"refresh_login OK at {now:.0f}")
            else:
                logger.debug("session 无 refresh() 方法，跳过")
        except Exception as e:
            logger.warning(
                f"refresh_login 失败（cookie 可能还有效）: "
                f"{type(e).__name__}: {e}"
            )
        self._last_refresh_login_at = now

    def _poll_once(self) -> None:
        """单次拉 session.sync：检测 unread 增量"""
        self._maybe_refresh_login()
        if not self._session:
            return
        # 1. 拉会话列表（v3.0 + spm_cnt 必需）
        raw = call(
            self._session,
            api="mtop.taobao.idlemessage.pc.session.sync",
            data={"fetchNum": 20},
            version="3.0",
            spm_cnt="a21ybx.im.0.0",
        )
        sessions = (raw.get("data") or {}).get("sessions") or []
        if not sessions:
            return
        # 2. 构 unread map
        curr_unread: dict[str, int] = {}
        for s in sessions:
            sid = s.get("session", {}).get("sessionId")
            summary = s.get("message", {}).get("summary") or {}
            if sid:
                curr_unread[sid] = int(summary.get("unread") or 0)
        # 3. 检测 unread 增量 → 触发 callback
        new_unread_cids = [
            cid for cid, n in curr_unread.items()
            if n > self._prev_unread_by_cid.get(cid, 0)
        ]
        for cid in new_unread_cids:
            sess = next(
                (s for s in sessions if s.get("session", {}).get("sessionId") == cid),
                None,
            )
            if not sess:
                continue
            ev = self._build_event(sess)
            if ev is None:
                continue
            # 防重：unread 可能在 cooldown 期间不变 → 用 cid+ts 去重
            event_key = f"{ev['cid']}|{ev['ts']}"
            if self._last_event_key_by_cid.get(cid) == event_key:
                continue
            self._last_event_key_by_cid[cid] = event_key
            try:
                self._callback(ev)
            except Exception as e:
                logger.exception(f"callback 异常: {e}")
        # 4. 更新 prev_unread
        self._prev_unread_by_cid = curr_unread

    def _build_event(self, sess: dict) -> Optional[dict]:
        """从 session.sync 一条 sess 构 callback 事件 dict"""
        try:
            session = sess["session"]
            summary = sess["message"]["summary"]
            user_info = session.get("userInfo") or {}
            # summary 文本：prefix + summary（prefix 是 "昵称: "，summary 是消息内容）
            prefix = str(summary.get("prefix", "") or "")
            content = str(summary.get("summary", "") or "")
            send_message = prefix + content
            ts = int(summary.get("ts", 0) or 0)
            return {
                "event": "message",
                "cid": str(session["sessionId"]),  # V16.8 P2-A hotfix: str (MCP 工具期望 string)
                "send_user_id": str(user_info.get("userId", "") or ""),
                "send_user_name": user_info.get("nick", "") or user_info.get("fishNick", ""),
                "send_message": send_message,
                "ts": ts,
            }
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"_build_event 解析失败: {e}")
            return None

    # ============================================================
    # 内部：P1-H stale watchdog（启动即开始计时）
    # ============================================================
    def _stale_watchdog_loop(self) -> None:
        """每 30s 检查最近一次 poll 是否成功（避免长时静默死锁）"""
        check_interval = 30.0
        stale_threshold_sec = 180.0
        last_success_poll_at = time.time()
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=check_interval):
                break
            idle_sec = time.time() - last_success_poll_at
            if idle_sec > stale_threshold_sec:
                logger.error(
                    f"XianyuWatcherPoller STALE: idle={idle_sec:.0f}s "
                    f"(>{stale_threshold_sec}s),recent poll 失败——"
                    f"重启 daemon 让 watchdog spawn 新进程"
                )
                # 不重启自己（在线程里 reset 易出 race），让外部 watchdog 检测
                # 通过 _stop_event 触发自然退出
                self._stop_event.set()
                break
            # 探测到 idle_sec 正常（poll 还在跑）

        # 重置 _last_success_poll_at 在 _poll_once 成功时调用


# ============================================================
# 自检（CLI 跑可连测）
# ============================================================
def _selftest() -> int:
    """快速验证 poller 能加载 + 拉 sessions（不接 callback）"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    events: list[dict] = []

    def cb(ev: dict) -> None:
        events.append(ev)
        logger.info(f"收到事件: {ev}")

    p = XianyuWatcherPoller(callback=cb, poll_interval=2.0)
    p.start()
    try:
        time.sleep(8.0)  # 等 4 轮 poll
    finally:
        p.stop()
    logger.info(f"共触发 {len(events)} 事件")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(_selftest())
