"""
ManualModeManager — 会话人工接管模式（V18 抄 XianyuAutoAgent main.py）

参考实现（main.py:263-298 + 479-483）：
  - 卖家发切换关键词（env TOGGLE_KEYWORDS，默认"。"）→ 切换该会话接管状态
  - 接管超时（env MANUAL_MODE_TIMEOUT，默认 3600s）后自动恢复自动回复
  - 接管期间：买家消息记入上下文，但不自动回复

我们的差异：
  - 卖家消息由 XianyuWsClient 通过 seller_callback 送达本管理器
    （参考项目在主循环里直接判 send_user_id == myid）
  - 状态内存态即可（重启清零，与参考一致——参考也没持久化接管状态）
"""
from __future__ import annotations

import os
import threading
import time


class ManualModeManager:
    """会话人工接管管理器（线程安全）"""

    def __init__(
        self,
        toggle_keywords: str | None = None,
        timeout_seconds: int | None = None,
    ):
        # 抄参考：TOGGLE_KEYWORDS 默认"。"（卖家发个句号就接管，超方便）
        if toggle_keywords is None:
            toggle_keywords = os.getenv("TOGGLE_KEYWORDS", "。")
        self._toggle_keywords = {
            kw.strip() for kw in toggle_keywords.split(",") if kw.strip()
        } or {"。"}
        if timeout_seconds is None:
            timeout_seconds = int(os.getenv("MANUAL_MODE_TIMEOUT", "3600"))
        self._timeout = float(timeout_seconds)
        self._manual: dict[str, float] = {}  # cid → 进入接管的时间
        # RLock：toggle() 持锁时会调 is_manual()（内部也拿锁），
        # 用非重入 Lock 会自死锁（V18 自测抓到）
        self._lock = threading.RLock()

    def is_toggle_keyword(self, text: str) -> bool:
        """是否为切换关键词（整条精确匹配，抄 check_toggle_keywords）"""
        return (text or "").strip() in self._toggle_keywords

    def handle_seller_message(self, cid: str, text: str) -> str:
        """处理卖家消息：若是切换关键词则切换接管状态

        Returns:
            "manual" | "auto"（发生了切换）| ""（非切换消息）
        """
        if not self.is_toggle_keyword(text):
            return ""
        mode = self.toggle(cid)
        return mode

    def toggle(self, cid: str) -> str:
        """切换接管状态（抄 toggle_manual_mode）"""
        with self._lock:
            if self.is_manual(cid):
                self._manual.pop(cid, None)
                return "auto"
            self._manual[cid] = time.time()
            return "manual"

    def enter(self, cid: str) -> None:
        """显式进入接管（后台工作台触发）"""
        with self._lock:
            self._manual[cid] = time.time()

    def exit(self, cid: str) -> None:
        """显式退出接管（后台工作台触发）"""
        with self._lock:
            self._manual.pop(cid, None)

    def is_manual(self, cid: str) -> bool:
        """会话是否处于接管中（超时自动恢复，抄 is_manual_mode）"""
        with self._lock:
            entered = self._manual.get(cid)
            if entered is None:
                return False
            if time.time() - entered > self._timeout:
                # 超时自动恢复（抄参考实现）
                self._manual.pop(cid, None)
                return False
            return True


__all__ = ["ManualModeManager"]
