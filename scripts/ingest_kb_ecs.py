#!/usr/bin/env python
"""
ECS 主机端跑：灌知识库到 Qdrant（不需要 admin 鉴权）
用法：python3 ingest_kb_ecs.py
"""
import json
import logging
import os
import sys
from pathlib import Path

# =============================================================
# ECS 主机环境（容器外，连 Docker MySQL/Qdrant）
# =============================================================
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://root:rootpass_cs_2026@127.0.0.1:3307/customer_service?charset=utf8mb4")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:6333")
os.environ.setdefault("QDRANT_COLLECTION", "faq_v1")

# 让 backend 包可 import
BACKEND_DIR = Path("/opt/customer-service/backend")
sys.path.insert(0, str(BACKEND_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest_kb")

from app.services.rag.ingest import ingest_text  # noqa: E402

KB_DIR = Path("/data/kb/ecommerce_kb")

KB_FILES = [
    "policy_return.json",
    "policy_warranty.json",
    "policy_shipping.json",
    "policy_payment.json",
    "policy_promotion.json",
    "policy_account.json",
    "policy_invoice.json",
    "policy_escalation.json",
    "faq_top20.json",
    "faq_product_sku.json",
    "faq_warranty.json",
    "products.json",
]


def main():
    if not KB_DIR.exists():
        logger.error(f"KB 目录不存在: {KB_DIR}")
        sys.exit(1)

    total = 0
    for filename in KB_FILES:
        path = KB_DIR / filename
        if not path.exists():
            logger.warning(f"跳过（不存在）: {filename}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # 商品列表 vs 知识条目
            items = data.get("items") or []
            if not items and "products" in data:
                # products.json 走商品路径
                for p in data["products"]:
                    text = f"{p.get('name', '')}\nSKU: {p.get('sku', '')}\n价格: {p.get('price', 0)}元\n规格: {p.get('attrs', {})}\n库存: {p.get('stock', 0)}"
                    result = ingest_text(
                        text=text,
                        source=f"product_{p.get('sku', '')}",
                        chunk_size=500,
                        overlap=50,
                        uploader_id=1,
                        title=p.get("name", ""),
                        description=f"商品 - {p.get('sku', '')}",
                    )
                    total += result.get("ingested_chunks", 0)
                continue
            for item in items:
                source = item.get("source") or f"{filename}:{item.get('sku', 'n/a')}"
                title = item.get("title") or item.get("name") or filename
                text = item.get("text") or ""
                if not text and "attrs" in item:
                    text = f"{item.get('name', '')}\nSKU: {item.get('sku', '')}\n价格: {item.get('price', 0)}元\n规格: {item.get('attrs', {})}"
                if not text:
                    logger.warning(f"  空 text, 跳过: {source}")
                    continue
                result = ingest_text(
                    text=text,
                    source=source,
                    chunk_size=500,
                    overlap=50,
                    uploader_id=1,
                    title=title,
                    description=item.get("description") or filename,
                )
                total += result.get("ingested_chunks", 0)
            logger.info(f"OK: {filename}")
        except Exception as e:
            logger.exception(f"FAIL: {filename}: {e}")

    logger.info(f"=== 全部完成: {total} chunks ingested ===")


if __name__ == "__main__":
    main()