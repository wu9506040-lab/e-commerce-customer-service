#!/usr/bin/env python
"""V13（2.1'）孤儿 source 清扫：删除 Qdrant 里"文件集已不存在"的存量点。

背景（2026-10-10 审计）：线上 202 点中 17 个 source 在 docs/ecommerce_kb/
文件里已不存在（admin_test、*.md 遗留命名、product_sku001-009 等历史世代）。
ingest_text 已加 delete-before-write（同 source 重灌自动换代），但"文件里
已消失的 source"不会被任何一次 reingest 触达——需要本脚本按集合差清扫。

用法：
    PYTHONPATH=backend python scripts/prune_orphan_sources.py           # dry-run 报告
    PYTHONPATH=backend python scripts/prune_orphan_sources.py --apply   # 真删
"""
import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# dotenv 三候选（直连型脚本惯例）
from dotenv import load_dotenv  # noqa: E402
for _p in (PROJECT_ROOT / "deploy" / ".env.dev", PROJECT_ROOT / ".env",
           PROJECT_ROOT / "backend" / ".env"):
    if _p.exists():
        load_dotenv(_p)
        break

from qdrant_client.models import Filter, FieldCondition, FilterSelector, MatchAny  # noqa: E402

from app.clients.qdrant import get_client, QDRANT_COLLECTION  # noqa: E402

KB_DIR = PROJECT_ROOT / "docs" / "ecommerce_kb"


def expected_sources() -> set:
    """当前文件集认定的合法 source（与 ingest_ecommerce_kb.py 同一数据源）。"""
    srcs = set()
    for f in sorted(KB_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[WARN] JSON parse fail: {f.name}")
            continue
        for item in data.get("items", []):
            s = item.get("source")
            if s:
                srcs.add(s)
    return srcs


def actual_sources() -> dict:
    """scroll 全库取 source 分布：{source: 点数}。"""
    client = get_client()
    dist = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=QDRANT_COLLECTION,
            with_payload=["source"],
            limit=1000,
            offset=offset,
        )
        for p in points:
            s = (p.payload or {}).get("source", "<no-source>")
            dist[s] = dist.get(s, 0) + 1
        if offset is None:
            break
    return dist


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删除孤儿点（默认 dry-run）")
    args = ap.parse_args()

    exp = expected_sources()
    dist = actual_sources()
    total_points = sum(dist.values())
    orphans = sorted(set(dist) - exp)
    orphan_points = sum(dist[s] for s in orphans)

    print(f"collection={QDRANT_COLLECTION} points={total_points} sources={len(dist)}")
    print(f"expected sources (files)={len(exp)}")
    print(f"orphan sources={len(orphans)} orphan_points={orphan_points}")
    for s in orphans:
        print(f"  [ORPHAN] {s}  points={dist[s]}")

    if not orphans:
        print("PRUNE: nothing to do")
        return 0
    if not args.apply:
        print("PRUNE: dry-run (pass --apply to delete)")
        return 0

    client = get_client()
    client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="source", match=MatchAny(any=orphans))])
        ),
        wait=True,
    )
    print(f"PRUNE: deleted {orphan_points} points from {len(orphans)} orphan sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
