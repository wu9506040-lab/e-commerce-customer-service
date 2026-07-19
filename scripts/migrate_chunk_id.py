#!/usr/bin/env python3
"""
P1-1 chunk_id 迁移脚本：旧 ID（基于下标）→ 新 ID（基于内容 hash）

# 为什么需要这个脚本

旧 `point_id = uuid5(source + ":" + i)` 基于下标，导致：
- source 中增/删 chunk → 后续所有 ID 整体偏移
- 重跑 ingest 时旧 chunk 的 ID 变了 → 删除旧点失败 → 累积脏点

新逻辑 `point_id = uuid5(source + ":" + chunk_hash[:32])` 基于内容 sha256，
同一 source 的同一段文本永远得到同一 ID → 重跑幂等、增量更新安全。

切换瞬间 Qdrant 会同时存在旧 ID 和新 ID 两份点（如果不迁移）。本脚本作用：
1. 拉全 collection
2. 按内容 hash 重新生成 ID
3. dry-run：只打印迁移统计 + 前 N 个示例，不动数据
4. apply：upsert 新点 + delete 旧点；失败记录 orphan 报告

# 用法

```bash
# 1. 先 dry-run 看看要迁移多少点（不会动数据）
python scripts/migrate_chunk_id.py --dry-run

# 2. 确认无问题后真正迁移
python scripts/migrate_chunk_id.py --apply

# 3. 指定 collection（默认 knowledge_base）
python scripts/migrate_chunk_id.py --dry-run --collection my_collection

# 4. 指定 Qdrant URL（默认从 .env.dev 读 QDRANT_URL）
QDRANT_URL=http://localhost:6333 python scripts/migrate_chunk_id.py --dry-run
```

# 风险与回滚

- apply 模式是不可逆操作（删了旧点）
- 迁移前建议 Qdrant snapshot（`curl -X POST http://qdrant:6333/collections/{name}/snapshots`）
- 失败时：upsert 成功但 delete 失败 → 记录到 orphan_report.json → 人工清理
- 开关 RAG_CHUNK_ID_BY_CONTENT_HASH=True 已经让新数据走新逻辑；迁移只清旧数据

# 数据库变更分级

按 CLAUDE.md §9.4.4：DB L2（数据迁移 + 删旧点）
- 备份 snapshot：迁移前必做
- 灰度脚本：dry-run → apply 两步走
- 可回滚：Qdrant snapshot restore
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("migrate_chunk_id")

# =============================================================
# 配置（CLAUDE.md §2 #5：禁止硬编码，从环境变量读）
# =============================================================
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
DEFAULT_COLLECTION = os.getenv("QDRANT_COLLECTION", "knowledge_base")

NAMESPACE = uuid.NAMESPACE_DNS
SCROLL_PAGE_SIZE = 100  # Qdrant scroll 单批大小
PREVIEW_COUNT = 5       # dry-run 打印前 N 个示例迁移对
ORPHAN_REPORT_PATH = Path(__file__).resolve().parent.parent / "docs" / "reports" / "migrate_chunk_id_orphans.json"


def _compute_new_id(source: str, text: str) -> str:
    """按 P1-1 新逻辑重算 chunk_id：基于内容 sha256

    与 ingest.py 内部生成逻辑完全一致（保证迁移结果与新 ingest 输出一致）。
    """
    chunk_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    return str(uuid.uuid5(NAMESPACE, f"{source}:{chunk_hash}"))


def _scroll_all_points(client, collection_name: str) -> List[Dict[str, Any]]:
    """scroll 整个 collection，返回所有点（带 payload 不带 vector）

    Qdrant scroll 返回 offset 用于翻页；用 offset=None 表示翻页完毕。
    """
    all_points: List[Dict[str, Any]] = []
    offset: Optional[Any] = None

    while True:
        points, next_offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=None,
            limit=SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_points.extend(points)

        if next_offset is None:
            break
        offset = next_offset

    logger.info(f"scroll 完成: collection={collection_name}, total={len(all_points)}")
    return all_points


def _compute_migration_pairs(points: List[Any]) -> List[Tuple[str, str, str]]:
    """对每个点算 (point_id, new_id, source) 三元组

    - point_id 当前 ID（str 化的 uuid）
    - new_id 按 P1-1 逻辑重算
    - source 用于诊断日志
    - 跳过没有 source/text payload 的点（旧格式异常数据）
    """
    pairs: List[Tuple[str, str, str]] = []
    skipped = 0
    for p in points:
        payload = getattr(p, "payload", None) or {}
        text = payload.get("text", "")
        source = payload.get("source", "")
        if not text or not source:
            skipped += 1
            continue
        current_id = str(p.id)
        new_id = _compute_new_id(source, text)
        if current_id != new_id:
            pairs.append((current_id, new_id, source))

    if skipped:
        logger.warning(f"跳过 {skipped} 个 payload 不完整的点（无 text/source）")
    return pairs


def _dry_run(collection_name: str) -> int:
    """dry-run：只计算迁移对 + 打印统计 + 前 N 个示例，不动数据"""
    from qdrant_client import QdrantClient

    client = QdrantClient(url=QDRANT_URL, timeout=30.0)
    points = _scroll_all_points(client, collection_name)
    pairs = _compute_migration_pairs(points)

    total = len(points)
    to_migrate = len(pairs)
    unchanged = total - to_migrate

    print("=" * 60)
    print(f"[DRY-RUN] chunk_id 迁移统计")
    print(f"  collection:    {collection_name}")
    print(f"  Qdrant URL:    {QDRANT_URL}")
    print(f"  总点数:        {total}")
    print(f"  需迁移:        {to_migrate}")
    print(f"  无变化（已对）: {unchanged}")
    print("=" * 60)

    if pairs:
        print(f"\n前 {min(PREVIEW_COUNT, len(pairs))} 个迁移示例:")
        for old_id, new_id, source in pairs[:PREVIEW_COUNT]:
            print(f"  {source}")
            print(f"    old: {old_id}")
            print(f"    new: {new_id}")

    print(f"\n执行迁移: python scripts/migrate_chunk_id.py --apply")
    return 0


def _apply(collection_name: str) -> int:
    """apply：upsert 新点 + delete 旧点；失败记录 orphan 报告"""
    from qdrant_client import QdrantClient
    from qdrant_client.models import PointStruct

    client = QdrantClient(url=QDRANT_URL, timeout=30.0)
    points = _scroll_all_points(client, collection_name)
    pairs = _compute_migration_pairs(points)

    logger.info(f"开始迁移: total={len(pairs)} 对")

    success = 0
    failed: List[Dict[str, Any]] = []  # 失败对：upsert 成功但 delete 失败 → 孤儿新点

    for old_id, new_id, source in pairs:
        try:
            # 找到旧点（payload + vector），用新 ID upsert 进去
            old_points = client.retrieve(
                collection_name=collection_name,
                ids=[old_id],
                with_payload=True,
                with_vectors=True,
            )
            if not old_points:
                logger.warning(f"旧点不存在（已被删？）: old_id={old_id}, source={source}")
                continue
            old_point = old_points[0]

            # 1. 用新 ID upsert
            new_point = PointStruct(
                id=new_id,
                vector=old_point.vector,
                payload=old_point.payload,
            )
            client.upsert(collection_name=collection_name, points=[new_point], wait=True)

            # 2. 删旧 ID
            try:
                client.delete(
                    collection_name=collection_name,
                    points_selector=[old_id],
                    wait=True,
                )
            except Exception as delete_err:
                # upsert 成功但 delete 失败 → 记录为孤儿（新 ID 已存在，旧 ID 也还在）
                logger.exception(f"delete 失败: old_id={old_id}, new_id={new_id}, source={source}")
                failed.append({
                    "old_id": old_id,
                    "new_id": new_id,
                    "source": source,
                    "error": f"{type(delete_err).__name__}: {str(delete_err)[:200]}",
                })
                continue

            success += 1
            if success % 100 == 0:
                logger.info(f"迁移进度: {success}/{len(pairs)}")

        except Exception as e:
            logger.exception(f"迁移失败: old_id={old_id}, source={source}")
            failed.append({
                "old_id": old_id,
                "new_id": new_id,
                "source": source,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            })

    print("=" * 60)
    print(f"[APPLY] chunk_id 迁移完成")
    print(f"  成功: {success}/{len(pairs)}")
    print(f"  失败: {len(failed)}")
    print("=" * 60)

    if failed:
        # 写 orphan 报告
        ORPHAN_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        ORPHAN_REPORT_PATH.write_text(
            json.dumps(
                {
                    "collection": collection_name,
                    "qdrant_url": QDRANT_URL,
                    "total_failed": len(failed),
                    "orphans": failed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n孤儿报告写入: {ORPHAN_REPORT_PATH}")
        print("  ⚠️  这些点的 new_id 已写入但 old_id 未删 → 需要人工清理")
        print("  ⚠️  或者从 Qdrant snapshot 恢复后重跑")

    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P1-1 chunk_id 迁移：旧（基于下标）→ 新（基于内容 hash）",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只计算 + 打印，不动数据")
    mode.add_argument("--apply", action="store_true", help="真正迁移（不可逆）")
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help=f"Qdrant collection 名（默认 {DEFAULT_COLLECTION}）",
    )
    args = parser.parse_args()

    logger.info(f"QDRANT_URL={QDRANT_URL}, collection={args.collection}")

    if args.dry_run:
        return _dry_run(args.collection)
    else:
        return _apply(args.collection)


if __name__ == "__main__":
    sys.exit(main())