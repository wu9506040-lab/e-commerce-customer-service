#!/usr/bin/env python
"""V14（2.4）badcase 池 → 评测集增量合入（人工审核后执行，半自动的"半"在这）

流程：
    1. 读 badcase_candidates 中 status=reviewed 的行
    2. 解析稳定键 source：expected_source 优先，缺省回退当轮检索现场首个 source；
       两者皆无 → FAIL（没依据就不合入，宁缺毋滥）
    3. 按 source 在 Qdrant 现库 scroll 出真实 point id 作为 relevant_doc_id
       （doc_id 会随洗库漂移，source 才是稳定键——WP2 调研结论）
    4. 合入 eval JSON 数组（按 query 精确去重），--apply 时先备份 .bak-<ts>
    5. 回写池内状态 merged（badcase_service.mark_merged，状态机锁防重复合入）

默认 dry-run（只报告不动数据）——飞轮闭环演示走 dry-run + 临时 --eval-file，
不污染 330 题基线。

用法（项目根目录，宿主机指向 docker 映射端口）：
    DATABASE_URL='mysql+pymysql://cs_user:<pwd>@localhost:3307/customer_service?charset=utf8mb4' \
    PYTHONIOENCODING=utf-8 python scripts/merge_badcase_to_eval.py [--dry-run|--apply]
                          [--limit 50] [--eval-file data/eval_set_v1.json]
报告：data/badcase_merge_report.json
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# dotenv 三候选（直连型脚本惯例）
try:
    from dotenv import load_dotenv  # type: ignore
    for env_file in [
        BACKEND_DIR / ".env",
        PROJECT_ROOT / "deploy" / ".env.dev",
        PROJECT_ROOT / ".env",
    ]:
        if env_file.exists():
            load_dotenv(env_file)
except ImportError:
    pass

import urllib.request

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models.badcase import STATUS_REVIEWED, BadcaseCandidate
from app.services import badcase_service

DEFAULT_EVAL = PROJECT_ROOT / "data" / "eval_set_v1.json"
DEFAULT_QDRANT = os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/")


def _qdrant_post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_collection(qdrant_url: str) -> str:
    """现库单集合是 knowledge_base；容错取实际存在者，不硬编码赌名字。"""
    with urllib.request.urlopen(f"{qdrant_url}/collections", timeout=10) as resp:
        names = [c["name"] for c in json.loads(resp.read())["result"]["collections"]]
    if "knowledge_base" in names:
        return "knowledge_base"
    if len(names) == 1:
        return names[0]
    raise RuntimeError(f"Qdrant 集合无法确定: {names}")


def resolve_doc_id(qdrant_url: str, collection: str, source: str) -> Optional[str]:
    """按稳定键 source 取一个真实 point id（同多点取首个）。"""
    data = _qdrant_post(f"{qdrant_url}/collections/{collection}/points/scroll", {
        "filter": {"must": [{"key": "source", "match": {"value": source}}]},
        "limit": 1,
        "with_payload": False,
    })
    points = (data.get("result") or {}).get("points") or []
    return str(points[0]["id"]) if points else None


def resolve_source(row: BadcaseCandidate) -> Tuple[Optional[str], str]:
    if row.expected_source:
        return row.expected_source.strip(), "expected_source"
    for ctx in (row.snapshot or {}).get("top_contexts") or []:
        if ctx.get("source"):
            return ctx["source"], "snapshot_fallback"
    return None, "no_source"


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="只报告不合入（默认）")
    mode.add_argument("--apply", action="store_true", help="真实合入+备份+回写 merged")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--eval-file", type=Path, default=DEFAULT_EVAL)
    ap.add_argument("--qdrant", default=DEFAULT_QDRANT)
    args = ap.parse_args()
    dry = not args.apply

    url = os.getenv("DATABASE_URL")
    if not url:
        print("FAIL: 需要 DATABASE_URL 环境变量（宿主机指向 docker 映射端口）")
        return 1
    # dotenv 可能带进 compose 内部主机名 mysql（宿主机不可达）——提示优先显式 env
    if "@mysql:" in url:
        print("FAIL: DATABASE_URL 用的是容器内主机名 mysql，宿主机请显式传 localhost:3307")
        return 1
    engine = create_engine(url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)

    eval_path: Path = args.eval_file
    if not eval_path.exists():
        print(f"FAIL: 评测集不存在 {eval_path}")
        return 1
    eval_entries = json.loads(eval_path.read_text(encoding="utf-8"))
    if not isinstance(eval_entries, list):
        print("FAIL: 评测集不是 JSON 数组（gen_eval_set 格式约定）")
        return 1
    existing_queries = {e.get("query") for e in eval_entries}
    collection = pick_collection(args.qdrant)

    report = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
              "dry_run": dry, "eval_file": str(eval_path),
              "reviewed_scanned": 0, "planned": [], "skipped_dup": [],
              "failed": [], "merged_ids": []}
    with Session() as db:
        rows = db.execute(
            select(BadcaseCandidate)
            .where(BadcaseCandidate.status == STATUS_REVIEWED,
                   BadcaseCandidate.deleted == 0)
            .order_by(BadcaseCandidate.id.asc())
            .limit(args.limit)
        ).scalars().all()
        for row in rows:
            report["reviewed_scanned"] += 1
            source, how = resolve_source(row)
            if not source:
                print(f"FAIL id={row.id} no-source: 无法确定应命中源，不合入")
                report["failed"].append({"id": row.id, "reason": "no_source"})
                continue
            doc_id = resolve_doc_id(args.qdrant, collection, source)
            if not doc_id:
                print(f"FAIL id={row.id} source-not-in-KB: {source}（先入库再合入）")
                report["failed"].append({"id": row.id, "reason": f"source_not_in_KB:{source}"})
                continue
            if row.query in existing_queries:
                print(f"SKIP id={row.id} dup-query: 评测集已有同问句")
                report["skipped_dup"].append({"id": row.id, "query": row.query})
                continue
            entry = {"query": row.query, "relevant_doc_id": doc_id, "source": source}
            report["planned"].append({"id": row.id, "via": how, "entry": entry})
            print(f"PASS id={row.id} via={how} source={source}")
            if dry:
                continue
            # 真实合入：先回写状态（状态机失败则不动评测集），再落文件
            badcase_service.mark_merged(
                db, badcase_id=row.id,
                note=f"merge ts={report['ts']} via={how} eval={eval_path.name}")
            eval_entries.append(entry)
            existing_queries.add(row.query)
            report["merged_ids"].append(row.id)

        if not dry and report["merged_ids"]:
            backup = eval_path.with_suffix(
                eval_path.suffix + f".bak-{dt.datetime.now():%Y%m%d%H%M%S}")
            backup.write_text(eval_path.read_text(encoding="utf-8"), encoding="utf-8")
            eval_path.write_text(
                json.dumps(eval_entries, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            print(f"BACKUP -> {backup.name}")
            print(f"WROTE  -> {eval_path}（现 {len(eval_entries)} 条）")

    out = PROJECT_ROOT / "data" / "badcase_merge_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"{'DRY-RUN' if dry else 'APPLY'} scanned={report['reviewed_scanned']} "
          f"planned={len(report['planned'])} dup={len(report['skipped_dup'])} "
          f"failed={len(report['failed'])}")
    print(f"REPORT -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
