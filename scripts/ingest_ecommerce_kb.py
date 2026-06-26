"""
batch_ingest.py - 批量入库电商知识库数据到 Qdrant + MySQL

遍历 docs/ecommerce_kb/*.json，调用现有 app.services.rag.ingest.ingest_text
入库到 Qdrant（向量）+ MySQL（metadata，§11 write-through）。

幂等性：source 字段作为唯一标识，重跑不会重复入库（Qdrant uuid5 + MySQL UNIQUE 约束）。

按 CLAUDE.md Scope Lock：不修改现有 services/rag/ingest.py，仅作为调用方复用。

用法：
    # 在 backend/ 目录下
    python ../scripts/ingest_ecommerce_kb.py

    # 或在项目根目录
    PYTHONPATH=backend python scripts/ingest_ecommerce_kb.py

    # 或 docker 容器内
    docker compose exec api python /app/scripts/ingest_ecommerce_kb.py
"""
import json
import logging
import sys
from pathlib import Path

# 让脚本能找到 backend/app
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 加载 .env（QWEN_API_KEY / QDRANT_URL / MYSQL 等）
try:
    from dotenv import load_dotenv  # type: ignore
    env_files = [
        BACKEND_DIR / ".env",  # 后端 .env（如果有）
        PROJECT_ROOT / "deploy" / ".env.dev",  # docker compose 用的
        PROJECT_ROOT / ".env",  # 项目根 .env（如果有）
    ]
    for env_file in env_files:
        if env_file.exists():
            load_dotenv(env_file)
            logging.info(f"已加载环境变量: {env_file}")
            break
    else:
        logging.warning("未找到 .env 文件，依赖系统环境变量")
except ImportError:
    logging.warning("python-dotenv 未安装，跳过 .env 加载（依赖系统环境变量）")

from app.services.rag.ingest import ingest_text  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> int:
    """
    主入口

    Returns:
        退出码（0 成功 / 1 失败）
    """
    kb_dir = PROJECT_ROOT / "docs" / "ecommerce_kb"
    if not kb_dir.exists():
        logger.error(f"目录不存在: {kb_dir}")
        return 1

    files = sorted(kb_dir.glob("*.json"))
    if not files:
        logger.error(f"目录为空: {kb_dir}")
        return 1

    logger.info(f"找到 {len(files)} 个 JSON 数据文件:")
    for f in files:
        logger.info(f"  - {f.name}")

    total_items = 0
    success_items = 0
    failed_items = 0
    total_chunks = 0

    for file_path in files:
        logger.info(f"\n=== 处理: {file_path.name} ===")
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.error(f"  JSON 解析失败: {e}")
            failed_items += 1
            continue

        items = data.get("items", [])
        logger.info(f"  条目数: {len(items)}")

        for item in items:
            source = item.get("source")
            text = item.get("text")
            doc_type = item.get("doc_type", "manual")

            if not source or not text:
                logger.warning(f"  跳过无效 item: source={source!r}")
                continue

            total_items += 1
            try:
                result = ingest_text(
                    text=text,
                    source=source,
                    title=item.get("title"),
                    description=item.get("description"),
                    doc_type=doc_type,
                )
                chunks = result.get("ingested_chunks", 0)
                total_chunks += chunks
                success_items += 1
                logger.info(f"  [OK] {source:40s} -> {chunks} chunks")
            except Exception as e:
                failed_items += 1
                logger.error(f"  [FAIL] {source}: {type(e).__name__}: {e}")

    # 汇总
    logger.info("\n" + "=" * 60)
    logger.info("汇总")
    logger.info("=" * 60)
    logger.info(f"  文件数:       {len(files)}")
    logger.info(f"  总条目:       {total_items}")
    logger.info(f"  成功:         {success_items}")
    logger.info(f"  失败:         {failed_items}")
    logger.info(f"  入库 chunks:  {total_chunks}")

    return 0 if failed_items == 0 else 1


if __name__ == "__main__":
    sys.exit(main())