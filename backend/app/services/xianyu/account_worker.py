"""
AccountWorker — 单账号运行时（V18.2 多账号）

封装原 run_xianyu_daemon 单账号全部接线：
  XianyuWsClient(cookie_path) + DebounceQueue + ReplyScheduler + ManualMode
  + recorder 落库（带 account 列）+ 事件上行（带 account_id）+ 工单/通知（account 归属）

生命周期：build() → start()（自愈重试）→ stop()
状态：stopped / starting / running / error → AccountManager 上报 Redis
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


class AccountWorker:
    def __init__(self, account: dict, recorder, event_sink, chat_api_url: str):
        self.account_id = str(account.get("id", "a1"))
        self.name = str(account.get("name", self.account_id))
        self.cookie_path = str(account.get("cookie_path", ""))
        self.enabled = bool(account.get("enabled", True))
        self.recorder = recorder          # 共享 ConversationRecorder（写库带 account）
        self.event_sink = event_sink      # 共享 AdminBridge.publish_event
        self.chat_api_url = chat_api_url
        self.status = "stopped"
        self.last_error = ""
        self.started_at = 0.0
        self.watcher = None
        self.scheduler = None
        self.debounce = None
        self._stop_flag = threading.Event()

    # ------------------------------------------------------------------
    def build(self) -> None:
        """构造本账号全套组件（对应原 run_xianyu_daemon 单账号接线）"""
        from app.services.xianyu.debounce_queue import DebounceQueue
        from app.services.xianyu.manual_mode import ManualModeManager
        from app.services.xianyu.reply_scheduler import ReplyScheduler
        from app.services.xianyu.xianyu_ws_client import XianyuWsClient

        account = {"id": self.account_id, "name": self.name}
        watcher = XianyuWsClient(callback=lambda _e: None, cookie_path=self.cookie_path or None)
        scheduler = ReplyScheduler(
            xianyu_client=watcher,
            chat_api_url=self.chat_api_url,
            min_delay_seconds=float(os.environ.get("XIANYU_MIN_DELAY", "30")),
            max_delay_seconds=float(os.environ.get("XIANYU_MAX_DELAY", "180")),
            rate_limit_per_minute=int(os.environ.get("XIANYU_RATE_LIMIT_PER_MIN", "6")),
            manual_mode=ManualModeManager(),
            account_id=self.account_id,
        )
        debounce = DebounceQueue(fire_callback=scheduler.on_fire, window_seconds=4.0)
        if hasattr(watcher, "set_callback"):
            watcher.set_callback(debounce.on_message)
        elif hasattr(watcher, "_callback"):
            watcher._callback = debounce.on_message

        # 事件上行（带 account_id）+ AI 回复落库
        scheduler._event_sink = (
            lambda kind, data, _aid=self.account_id: self.event_sink(kind, {**data, "account_id": _aid})
        )
        scheduler._recorder = self.recorder

        # 人工后台：recorder 回调（落库带 account）+ toid 映射 + buyer 事件
        orig_cb = debounce.on_message

        def recording_cb(ev: dict) -> None:
            ev2 = {**ev, "account_id": self.account_id}
            try:
                self.recorder.record_buyer(
                    cid=str(ev.get("cid", "")),
                    toid=str(ev.get("send_user_id", "")),
                    nick=str(ev.get("send_user_name", "") or ""),
                    text=str(ev.get("text", "") or ""),
                    message_id=str(ev.get("message_id", "") or ""),
                    account=self.account_id,
                )
            except Exception as e:
                logger.debug(f"[{self.account_id}] recorder.record_buyer 失败: {e}")
            # V18.2.2 防双 AI 回环：自家账号互发的消息只落库不触发 AI
            own_getter = getattr(self, "own_unbs_getter", None)
            own_set = own_getter() if own_getter else set()
            sender = str(ev.get("send_user_id", ""))
            if sender and own_set and sender in own_set:
                logger.info(f"[{self.account_id}] 自家账号({sender})消息 → 只落库不自动回复")
                _bridge_cb = getattr(self, "_bridge_buyer_cb", None)
                if _bridge_cb:
                    _bridge_cb(ev2)
                return
            # buyer_message 事件由 bridge.record_buyer_event 发布（含 toid 映射维护）
            _bridge_cb = getattr(self, "_bridge_buyer_cb", None)
            if _bridge_cb:
                _bridge_cb(ev2)
            orig_cb(ev)

        if hasattr(watcher, "set_callback"):
            watcher.set_callback(recording_cb)
        elif hasattr(watcher, "_callback"):
            watcher._callback = recording_cb

        # 卖家切换接管（"。"）
        if scheduler._manual_mode is not None:
            def on_seller_message(ev: dict) -> None:
                mode = scheduler._manual_mode.handle_seller_message(
                    str(ev.get("cid", "")), str(ev.get("text", "") or "")
                )
                if mode == "manual":
                    logger.info(f"🔴 [{self.account_id}] 已接管会话 {ev.get('cid')}")
                elif mode == "auto":
                    logger.info(f"🟢 [{self.account_id}] 已恢复会话 {ev.get('cid')} 自动回复")

            watcher._seller_callback = on_seller_message

        self.watcher = watcher
        self.scheduler = scheduler
        self.debounce = debounce

    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动（自愈重试：失败 30s 后再试，进程不退）"""
        if self.watcher is None:
            self.build()
        self.status = "starting"
        self._stop_flag.clear()
        t = threading.Thread(target=self._run_with_heal, name=f"worker-{self.account_id}", daemon=True)
        t.start()

    def _run_with_heal(self) -> None:
        self.started_at = time.time()
        while not self._stop_flag.is_set():
            try:
                self.watcher.start()
                self.status = "running"
                self.last_error = ""
                logger.info(f"[{self.account_id}] {self.name} WebSocket 已启动")
                break
            except Exception as e:
                self.status = "error"
                self.last_error = f"{type(e).__name__}: {e}"
                logger.exception(f"[{self.account_id}] 启动失败，30s 自愈重试: {e}")
                if self._stop_flag.wait(30):
                    return

    def stop(self) -> None:
        self._stop_flag.set()
        self.status = "stopped"
        try:
            if self.watcher:
                self.watcher.stop()
            if self.debounce:
                self.debounce.shutdown()
        except Exception as e:
            logger.warning(f"[{self.account_id}] stop 异常: {e}")


__all__ = ["AccountWorker"]
