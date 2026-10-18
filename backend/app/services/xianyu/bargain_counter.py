"""
BargainCounter — 议价轮次计数（V18 抄 XianyuAutoAgent context_manager.chat_bargain_counts）

参考实现（context_manager.py:254-282）：
  SQLite 表 chat_bargain_counts(chat_id PK, count, last_updated)
  increment: INSERT ... ON CONFLICT(chat_id) DO UPDATE SET count = count + 1
  get: SELECT count

我们的差异：
  - 议价意图判定用本地关键词（抄 xianyu-auto-reply ai_reply_engine.detect_intent 的
    price_keywords），在 daemon 侧判——后端 /chat 的意图我们拿不到，关键词判定
    与参考项目的本地意图检测同一套路。
  - 计数注入 system prompt（"当前议价轮次：N"），配合 reply_scheduler 的让步
    策略指令实现梯度让步；动态温度（PriceAgent temp 公式）待 /chat 支持
    temperature 透传后接入。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime

from loguru import logger  # noqa: F401  保持与参考项目一致的日志风格入口

# 抄 xianyu-auto-reply ai_reply_engine.detect_intent 的价格关键词
PRICE_KEYWORDS = (
    "便宜", "优惠", "刀", "降价", "价格", "多少钱",
    "能少", "还能", "最低", "底价", "实诚价", "到100", "能到",
    "包个邮", "少点", "砍价",
)

# 议价计数过期时间：超过该时长未议价则从 0 重新计（参考项目无此逻辑，
# 我们补上——隔天的"多少钱"不该算同一轮议价）
_BARGAIN_EXPIRE_SECONDS = 3600.0


def is_price_message(text: str) -> bool:
    """本地判定买家消息是否议价意图（零 LLM 开销）"""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in PRICE_KEYWORDS)


class BargainCounter:
    """议价轮次计数器（SQLite 持久化，线程安全）"""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            # daemon cwd 是 backend/，data 在项目根
            db_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "..", "data", "bargain_counts.db",
            )
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        return conn

    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_bargain_counts (
                    chat_id TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def increment(self, chat_id: str) -> int:
        """轮次 +1（UPSERT，抄参考实现），返回最新轮次"""
        with self._lock:
            conn = self._connect()
            try:
                now = datetime.now().isoformat()
                conn.execute(
                    """
                    INSERT INTO chat_bargain_counts (chat_id, count, last_updated)
                    VALUES (?, 1, ?)
                    ON CONFLICT(chat_id)
                    DO UPDATE SET count = count + 1, last_updated = ?
                    """,
                    (chat_id, now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT count FROM chat_bargain_counts WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
                return int(row[0]) if row else 1
            finally:
                conn.close()

    def get_count(self, chat_id: str, expire_seconds: float = _BARGAIN_EXPIRE_SECONDS) -> int:
        """取当前轮次；超过 expire_seconds 未更新视为新一轮议价，返回 0"""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """SELECT count, last_updated FROM chat_bargain_counts
                       WHERE chat_id = ?""",
                    (chat_id,),
                ).fetchone()
                if not row:
                    return 0
                count, last_updated = int(row[0]), row[1]
                try:
                    age = time.time() - datetime.fromisoformat(last_updated).timestamp()
                except Exception:
                    return 0
                if age > expire_seconds:
                    return 0
                return count
            finally:
                conn.close()


_singleton: BargainCounter | None = None
_singleton_lock = threading.Lock()


def get_bargain_counter() -> BargainCounter:
    """单例（daemon 进程内）"""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = BargainCounter()
        return _singleton


__all__ = ["BargainCounter", "get_bargain_counter", "is_price_message", "PRICE_KEYWORDS"]
