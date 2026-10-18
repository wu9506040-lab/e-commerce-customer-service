"""
ItemCache — 商品信息本地缓存（V18 抄 XianyuAutoAgent context_manager.items 表
+ main.py.build_item_description / format_price）

参考实现：
  - XianyuApis.get_item_info：mtop.taobao.idle.pc.detail，data={"itemId":...}
  - context_manager：items 表存 itemDO 全量 JSON，API 只拉一次
  - main.build_item_description：提取 title/desc/价格区间/库存/SKU 摘要给 LLM
  - main.format_price：价格分转元

我们的差异：
  - mtop 调用复用 XianyuApis._post_mtop（自带 token 过期自愈重试）
  - SQLite 路径默认项目 data/ 目录
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime


def format_price(price) -> float:
    """价格分转元（抄 main.format_price）"""
    try:
        return round(float(price) / 100, 2)
    except (ValueError, TypeError):
        return 0.0


def build_item_description(item_info: dict) -> str:
    """构建商品描述 JSON（抄 main.build_item_description）"""
    clean_skus = []
    for sku in item_info.get("skuList", []) or []:
        specs = [
            p["valueText"] for p in (sku.get("propertyList", []) or [])
            if p.get("valueText")
        ]
        spec_text = " ".join(specs) if specs else "默认规格"
        clean_skus.append({
            "spec": spec_text,
            "price": format_price(sku.get("price", 0)),
            "stock": sku.get("quantity", 0),
        })

    valid_prices = [s["price"] for s in clean_skus if s["price"] > 0]
    if valid_prices:
        min_price, max_price = min(valid_prices), max(valid_prices)
        price_display = (
            f"¥{min_price}" if min_price == max_price
            else f"¥{min_price} - ¥{max_price}"
        )
    else:
        main_price = round(float(item_info.get("soldPrice", 0)), 2)
        price_display = f"¥{main_price}"

    summary = {
        "title": item_info.get("title", ""),
        "desc": item_info.get("desc", ""),
        "price_range": price_display,
        "total_stock": item_info.get("quantity", 0),
        "sku_details": clean_skus,
    }
    return json.dumps(summary, ensure_ascii=False)


class ItemCache:
    """商品信息缓存（SQLite items 表，抄参考 context_manager）"""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "..", "data", "item_cache.db",
            )
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    price REAL,
                    description TEXT,
                    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def get_item_info(self, item_id: str) -> dict | None:
        """缓存命中返回 itemDO dict，未命中 None"""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT data FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None
        except Exception:
            return None
        finally:
            conn.close()

    def save_item_info(self, item_id: str, item_data: dict) -> None:
        """存商品全量 JSON（抄 save_item_info）"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                price = float(item_data.get("soldPrice", 0) or 0)
                description = item_data.get("desc", "")
                data_json = json.dumps(item_data, ensure_ascii=False)
                now = datetime.now().isoformat()
                conn.execute(
                    """
                    INSERT INTO items (item_id, data, price, description, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(item_id)
                    DO UPDATE SET data = ?, price = ?, description = ?, last_updated = ?
                    """,
                    (item_id, data_json, price, description, now,
                     data_json, price, description, now),
                )
                conn.commit()
            finally:
                conn.close()

    def get_item_description(self, item_id: str, apis) -> str:
        """带缓存取商品描述：miss 时走 mtop 拉一次（apis = XianyuApis 实例）"""
        if not item_id or apis is None:
            return ""
        item_info = self.get_item_info(item_id)
        if item_info is None:
            result = apis.get_item_info(item_id)
            item_info = (
                result.get("data", {}).get("itemDO")
                if isinstance(result, dict) and "data" in result
                else None
            )
            if not item_info:
                return ""
            self.save_item_info(item_id, item_info)
        try:
            return build_item_description(item_info)
        except Exception:
            return ""


__all__ = ["ItemCache", "build_item_description", "format_price"]
