"""虚拟商品自动发货服务（V21 P0 · 业务打通演示）。

为什么这个模块重要：
- 企业客服 Agent 的核心 KPI 是**解决率**，解决率的前提是业务系统打通——
  光接聊天框的客服机器人是玩具。自动发货（虚拟商品）是电商客服
  深度接业务链路的标配能力（淘宝平台原生支持，非灰色）。
- 本服务跑在**自家商城**订单库上（有真实数据模型），不碰闲鱼交易
  （闲鱼无开放 API，写操作=风控暴露面——平台限制，非设计优越）。

与 ApprovalGate（V20 亮点2）的关系——这是它的第一个真实消费者：
- 自动发货是**写操作**，走完整票据门控：request → 用户/坐席确认 →
  verify_and_consume → 执行 → 审计。llm 重试改参数 = 无票拒绝。
- 幂等由两层保证：①票据 duplicate 拒绝 ②交付记录表按 order_no 唯一
  （重复请求返回原卡密，不重复扣库存）。

存储：独立 SQLite（data/delivery.db）——与 BargainCounter 同款先例。
表：delivery_cards（卡密库存）+ delivery_records（交付记录，order_no 唯一）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    card_secret TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'unused'   -- unused | used
);
CREATE INDEX IF NOT EXISTS idx_cards_sku_status ON delivery_cards(sku, status);
CREATE TABLE IF NOT EXISTS delivery_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,          -- 幂等键：一单只发一次
    user_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    card_secret TEXT NOT NULL,
    ticket_id TEXT NOT NULL,                -- 审批票据回链（V20 亮点2）
    delivered_at INTEGER NOT NULL,
    steps_json TEXT                         -- V21-P0.2：履约时间线（决策链快照）
);
"""


class DeliveryService:
    """卡密库存 + 交付记录（线程安全，单实例足够；多副本换 MySQL）。"""

    def __init__(self, db_path: str = "data/delivery.db"):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, check_same_thread=False)

    # ---------- 库存管理（运营侧） ----------

    def load_inventory(self, sku: str, secrets: list[str]) -> int:
        """导入卡密（幂等：card_secret UNIQUE，重复导入自动跳过）。返回新增数。"""
        with _lock, self._conn() as c:
            cur = c.executemany(
                "INSERT OR IGNORE INTO delivery_cards (sku, card_secret) VALUES (?, ?)",
                [(sku, s) for s in secrets],
            )
            return cur.rowcount

    def stock(self, sku: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM delivery_cards WHERE sku=? AND status='unused'",
                (sku,),
            ).fetchone()
            return int(row[0])

    # ---------- 发货核心 ----------

    def assign_card(self, order_no: str, user_id: int, sku: str, ticket_id: str) -> dict:
        """分配卡密并发货（幂等）。

        Returns:
            {"status": "delivered", "card_secret": ..., "duplicate": False} —— 正常发货
            {"status": "delivered", "card_secret": ..., "duplicate": True}  —— 重复请求返回原卡
            {"status": "out_of_stock"}                                      —— 无库存
        """
        with _lock, self._conn() as c:
            # 幂等第一层：同订单已发货 → 返回原卡密
            dup = c.execute(
                "SELECT card_secret FROM delivery_records WHERE order_no=?",
                (order_no,),
            ).fetchone()
            if dup:
                return {"status": "delivered", "card_secret": dup[0], "duplicate": True}

            # 分配一张未用卡密（SELECT 后 UPDATE，锁内原子）
            row = c.execute(
                "SELECT id, card_secret FROM delivery_cards "
                "WHERE sku=? AND status='unused' ORDER BY id LIMIT 1",
                (sku,),
            ).fetchone()
            if row is None:
                return {"status": "out_of_stock"}

            card_id, secret = row
            c.execute("UPDATE delivery_cards SET status='used' WHERE id=?", (card_id,))
            c.execute(
                "INSERT INTO delivery_records "
                "(order_no, user_id, sku, card_secret, ticket_id, delivered_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (order_no, user_id, sku, secret, ticket_id, int(time.time() * 1000)),
            )
        logger.info("[delivery] order=%s sku=%s 已发货 ticket=%s", order_no, sku, ticket_id)
        return {"status": "delivered", "card_secret": secret, "duplicate": False}

    def _ensure_steps_column(self) -> None:
        """旧库懒迁移：delivery_records 补 steps_json 列（幂等）。"""
        with self._conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(delivery_records)")}
            if "steps_json" not in cols:
                c.execute("ALTER TABLE delivery_records ADD COLUMN steps_json TEXT")

    def save_timeline(self, order_no: str, steps: list[dict], latency_ms: float) -> None:
        """V21-P0.2：持久化履约决策链快照（列表/详情页展示用）。"""
        self._ensure_steps_column()
        with self._conn() as c:
            c.execute(
                "UPDATE delivery_records SET steps_json=? WHERE order_no=?",
                (json.dumps({"steps": steps, "latency_ms": latency_ms}, ensure_ascii=False), order_no),
            )

    def get_record(self, order_no: str) -> Optional[dict]:
        self._ensure_steps_column()
        with self._conn() as c:
            row = c.execute(
                "SELECT order_no, sku, card_secret, delivered_at, steps_json "
                "FROM delivery_records WHERE order_no=?",
                (order_no,),
            ).fetchone()
        if row is None:
            return None
        rec = {
            "order_no": row[0], "sku": row[1],
            "card_secret": row[2], "delivered_at": row[3],
        }
        if row[4]:
            try:
                rec.update(json.loads(row[4]))
                rec["status"] = "delivered"
            except (TypeError, ValueError):
                pass
        return rec


def get_delivery_service() -> DeliveryService:
    """单例入口（db 路径可由测试 reset）。"""
    global _svc
    if _svc is None:
        with _lock:
            if _svc is None:
                from app.core.config import settings

                _svc = DeliveryService(getattr(settings, "DELIVERY_DB", "data/delivery.db"))
    return _svc


def reset_delivery_service() -> None:
    global _svc
    with _lock:
        _svc = None


_svc: Optional[DeliveryService] = None
