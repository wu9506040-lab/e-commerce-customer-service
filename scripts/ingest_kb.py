#!/usr/bin/env python3
"""
通用知识库入库流水线（V18 RAG 数据中台）

支持格式与解析（doc_parsers.py）：
  .pdf   页眉/页脚剔除、字号层级识别章节、表格转 markdown、图片引用导出、脚注成块
  .md    标题层级感知（章节路径保留）
  .txt   段落
  .json  原 FAQ 格式

流程：扫描 docs/ecommerce_kb/*
  → 解析（多格式 → 统一 items，带 page/section/kind 元数据）
  → 增量判断（source+content_hash，未变更零成本跳过）
  → 语义切分（句子边界贪心打包 ≤600 字 + 尾句重叠，绝不截断句中）
  → 批量向量化（text-embedding-v3）
  → Qdrant upsert（payload: channel/category/section/page/kind/content_hash）
  → MySQL knowledge_documents 镜像

渠道隔离：文件名/category 含 "xianyu" → channel="xianyu"，否则 "mall"
幂等：重复跑零成本；失败不清 state（下次重灌）

使用：
  python scripts/ingest_kb.py                 # 全量扫描（增量）
  python scripts/ingest_kb.py --file xx.pdf   # 指定文件
  python scripts/ingest_kb.py --force         # 忽略 hash 强制重灌
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from doc_parsers import parse_file, semantic_chunks  # noqa: E402

ENV_FILE = Path(__file__).resolve().parents[1] / "deploy" / ".env.dev"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openai import OpenAI  # noqa: E402

KB_DIR = Path(__file__).resolve().parents[1] / "docs" / "ecommerce_kb"
STATE_FILE = Path(__file__).resolve().parents[1] / "data" / ".ingest_state.json"
QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.getenv("COLLECTION", os.environ.get("QDRANT_COLLECTION", "knowledge_base"))
EMBEDDING_MODEL = "text-embedding-v3"

CHUNK_MAX_CHARS = 600


def content_hash(title: str, text: str) -> str:
    return hashlib.sha1(f"{title}\n{text}".encode("utf-8")).hexdigest()[:16]


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def embed_texts(texts: list[str]) -> list[list[float]]:
    client = OpenAI(
        api_key=os.environ.get("QWEN_API_KEY", ""),
        base_url=os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        timeout=30.0,
    )
    last_err = None
    for attempt in range(3):
        try:
            r = client.embeddings.create(
                model=EMBEDDING_MODEL, input=texts, encoding_format="float", timeout=30
            )
            return [d.embedding for d in r.data]
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 1.0 * (2 ** attempt)
            print(f"  [embed] 重试 {attempt+1}/3: {type(e).__name__}, waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"embedding 失败 3 次: {last_err}")


def qdrant_upsert(points: list[dict]) -> bool:
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{COLLECTION}/points?wait=true",
        data=json.dumps({"points": points}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.loads(resp.read())
            return True
    except Exception as e:
        print(f"  [qdrant] upsert 失败: {e}")
        return False


def mirror_mysql(source: str, title: str, doc_type: str,
                 total_chunks: int, total_chars: int, channel: str) -> None:
    """镜像 knowledge_documents（source 唯一键 upsert；channel 列自动补）"""
    try:
        import pymysql
        conn = pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("MYSQL_PORT", "3307")),  # host 映射端口（容器内 3306）
            user=os.environ.get("MYSQL_USER", "cs_user"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "customer_service"),
            charset="utf8mb4",
        )
    except Exception as e:
        print(f"  [mysql] 连接失败（跳过镜像）: {e}")
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name='knowledge_documents' "
                "AND column_name='channel'"
            )
            if cur.fetchone()[0] == 0:
                cur.execute(
                    "ALTER TABLE knowledge_documents ADD COLUMN channel VARCHAR(20) "
                    "NOT NULL DEFAULT 'mall'"
                )
            cur.execute(
                """
                INSERT INTO knowledge_documents
                    (source, title, doc_type, total_chunks, total_chars, channel)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    title=VALUES(title), doc_type=VALUES(doc_type),
                    total_chunks=VALUES(total_chunks), total_chars=VALUES(total_chars),
                    channel=VALUES(channel), update_time=NOW()
                """,
                (source, title, doc_type, total_chunks, total_chars, channel),
            )
        conn.commit()
    except Exception as e:
        print(f"  [mysql] 镜像失败: {e}")
    finally:
        conn.close()


def channel_of(category: str, filename: str) -> str:
    """渠道自动判定：xianyu 系 → 闲鱼专属 KB；其余 → 商城"""
    return "xianyu" if ("xianyu" in category or "xianyu" in filename) else "mall"


def main() -> int:
    force = "--force" in sys.argv
    extract_images = os.environ.get("INGEST_EXTRACT_IMAGES", "1") == "1"
    only_file = None
    if "--file" in sys.argv:
        only_file = sys.argv[sys.argv.index("--file") + 1]

    state = load_state()
    files = sorted(KB_DIR.glob("*.*")) if KB_DIR.exists() else []
    if only_file:
        files = [KB_DIR / only_file]
    files = [f for f in files if f.suffix.lower() in (".json", ".pdf", ".md", ".markdown", ".txt")]
    if not files:
        print(f"无可入库文件: {KB_DIR}")
        return 1

    total_points = total_skip = 0
    for f in files:
        if not f.exists():
            print(f"跳过（不存在）: {f.name}")
            continue
        try:
            category, items = parse_file(f, extract_images=extract_images)
        except Exception as e:
            print(f"{f.name}: 解析失败 {type(e).__name__}: {e}")
            continue
        channel = channel_of(str(category), f.name)
        new_points: list[dict] = []
        doc_hashes: list[str] = []

        for it in items:
            source = it["source"]
            title = it.get("title", "")
            text = it.get("text", "")
            meta = it.get("meta", {}) or {}
            chash = content_hash(title, text)
            doc_hashes.append(chash)
            if not force and state.get(source) == chash:
                total_skip += 1
                continue
            chunks = semantic_chunks(text, max_chars=CHUNK_MAX_CHARS)
            for ci, piece in enumerate(chunks):
                new_points.append({
                    "id": str(uuid4()),
                    "payload": {
                        "channel": channel,
                        "category": str(category),
                        "doc_type": "doc",
                        "source": source,
                        "title": title,
                        "text": piece,
                        "chunk_index": ci,
                        "section": str(meta.get("section", "")),
                        "page": meta.get("page"),
                        "kind": str(meta.get("kind", "text")),
                        "content_hash": chash,
                    },
                })
                state[source] = chash

        if not new_points:
            print(f"{f.name}: 无新增（channel={channel}）")
            continue

        print(f"{f.name}: 新增 {len(new_points)} points，channel={channel} "
              f"(kinds: {sorted({p['payload']['kind'] for p in new_points})})")
        texts = [p["payload"]["text"] for p in new_points]
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 10):
            vectors.extend(embed_texts(texts[i:i+10]))
        for p, v in zip(new_points, vectors):
            p["vector"] = v

        if qdrant_upsert(new_points):
            total_points += len(new_points)
            print(f"  ✅ upsert {len(new_points)} points → {COLLECTION}")
            mirror_mysql(
                source=f.name, title=f.stem, doc_type="file",
                total_chunks=len(new_points), total_chars=sum(len(t) for t in texts),
                channel=channel,
            )
        else:
            for it2 in items:
                state.pop(it2["source"], None)  # 失败不清 state（下次重灌）
            _ = doc_hashes

    save_state(state)
    print(f"\n完成: 新入库 points={total_points}, 跳过(未变更)={total_skip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
