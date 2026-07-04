#!/usr/bin/env python
"""
一次性灌知识库到 Qdrant（容器内运行，绕过 admin 鉴权）

用法：
    docker cp scripts/ingest_kb_to_qdrant.py customer-service-api:/app/
    docker exec -w /app customer-service-api python ingest_kb_to_qdrant.py
"""
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest_kb")

# kb 文件目录（容器外相对路径，容器内需要绝对路径）
KB_DIR = Path("/data/kb")  # 先 cp 到 /data/kb

from app.services.rag.ingest import ingest_text

KB_FILES = [
    ("policy_return.json", "退换货政策"),
    ("policy_warranty.json", "保修政策"),
    ("policy_shipping.json", "物流政策"),
    ("policy_payment.json", "支付政策"),
    ("policy_promotion.json", "促销政策"),
    ("policy_account.json", "账户政策"),
    ("policy_invoice.json", "发票政策"),
    ("policy_escalation.json", "升级处理"),
    ("faq_top20.json", "常见问答 Top20"),
    ("faq_product_sku.json", "商品 FAQ"),
    ("faq_warranty.json", "保修 FAQ"),
    ("products.json", "商品信息"),
]


def main():
    if not KB_DIR.exists():
        logger.error(f"KB 目录不存在: {KB_DIR}")
        sys.exit(1)

    total = 0
    for filename, label in KB_FILES:
        path = KB_DIR / filename
        if not path.exists():
            logger.warning(f"跳过（不存在）: {filename}")
            continue
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("items") or data.get("products") or []
            for item in items:
                source = item.get("source") or f"{filename}:{item.get('sku', 'n/a')}"
                title = item.get("title") or item.get("name") or label
                text = item.get("text") or ""
                # 商品条目无 text，用 attributes 拼
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
                    uploader_id=1,  # admin
                    title=title,
                    description=item.get("description") or label,
                )
                chunks = result.get("ingested_chunks", 0)
                total += chunks
                logger.info(f"  {source}: {chunks} chunks")
            logger.info(f"OK: {filename} ({label})")
        except Exception as e:
            logger.exception(f"FAIL: {filename}: {e}")

    logger.info(f"=== 全部完成: {total} chunks ingested ===")


if __name__ == "__main__":
    main()