"""
gen_eval_set.py - 合成 hit@K 评估集（query → relevant_doc_id）

读取 Qdrant 中所有 67 条知识库文档，让千问为每条文档生成 2-3 个
真实用户口吻的问题，写入 data/eval_set_v1.json。

输出格式：
    [
        {"query": "...", "relevant_doc_id": "uuid-xxx", "source": "policy_xxx"},
        ...
    ]

设计取舍：
- 每条 doc 生成 2-3 个问题 → 67 * 2.5 ≈ 167 条，足够做 hit@1/3/5/10
- 用 temperature=0.8 增加多样性，但控制 prompt 让问题贴近真实用户
- 严格 JSON 输出（带 system prompt + retry 一次）→ 避免解析失败
- ID 必须是 Qdrant 真实 point id（用于后续 eval_hitk.py 比对）

用法（在项目根目录）：
    PYTHONPATH=backend python scripts/gen_eval_set.py
    # 或 docker 容器内
    docker compose exec api python /app/scripts/gen_eval_set.py

注意：本脚本只读 Qdrant，不写入；只调 Qwen（生成 query），不调 embedding。
"""
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import List, Dict, Any

# 让脚本能找到 backend/app
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 加载 .env
try:
    from dotenv import load_dotenv  # type: ignore
    for env_file in [
        BACKEND_DIR / ".env",
        PROJECT_ROOT / "deploy" / ".env.dev",
        PROJECT_ROOT / ".env",
    ]:
        if env_file.exists():
            load_dotenv(env_file)
            break
except ImportError:
    pass

from qdrant_client import QdrantClient  # noqa: E402
from app.core.config import settings  # noqa: E402
# Sprint 4 收尾：core/qwen.py 改为 Provider 抽象入口
from app.core.providers.llm import get_llm_provider  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

OUTPUT_PATH = PROJECT_ROOT / "data" / "eval_set_v1.json"
COLLECTION = settings.QDRANT_COLLECTION

# =============================================================
# 1. 读取 Qdrant 全部文档
# =============================================================
def load_all_docs() -> List[Dict[str, Any]]:
    """
    从 Qdrant scroll 读取 collection 全部 point

    Returns:
        [{"id": str, "source": str, "text": str, "chunk_index": int}, ...]
    """
    client = QdrantClient(url=settings.QDRANT_URL, timeout=30.0)
    docs = []
    offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=COLLECTION,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            payload = p.payload or {}
            docs.append({
                "id": str(p.id),
                "source": payload.get("source", "unknown"),
                "chunk_index": payload.get("chunk_index", 0),
                "text": payload.get("text", ""),
            })
        if next_offset is None:
            break
        offset = next_offset
    logger.info(f"从 Qdrant '{COLLECTION}' 读取 {len(docs)} 条文档")
    return docs


# =============================================================
# 2. 调用千问生成查询
# =============================================================
GENERATION_PROMPT = """你是电商客服系统的测试数据生成助手。基于以下客服知识库文档，生成 3 个真实用户可能问的问题。

要求：
1. 问题必须是用户口吻（口语化、不完整句、可能有错别字）
2. 覆盖不同角度（如"流程类"问法、"条件类"问法、"例外情况"问法）
3. 不要直接复述文档标题，要用用户实际会用的表达
4. 每个问题 10-30 字
5. 严格输出 JSON 数组，不要任何额外文字

输出格式（仅 JSON，不要 markdown 包裹）：
[{{"query": "问题1"}}, {{"query": "问题2"}}, {{"query": "问题3"}}]

文档内容：
{text}"""


def generate_queries_for_doc(doc: Dict[str, Any]) -> List[str]:
    """
    为单条文档生成 2-3 个查询（带一次 retry）

    Returns:
        问题列表（生成失败返回空列表）
    """
    text = doc["text"][:800]  # 截断避免 prompt 过长
    prompt = GENERATION_PROMPT.format(text=text)

    messages = [
        {"role": "system", "content": "你只输出 JSON，不要任何解释。"},
        {"role": "user", "content": prompt},
    ]

    for attempt in range(2):
        try:
            result = get_llm_provider().chat(messages, temperature=0.8, max_tokens=500)
            reply = result["reply"].strip()

            # 尝试解析：先去掉可能的 markdown 包裹
            if reply.startswith("```"):
                reply = re.sub(r"^```(?:json)?\s*", "", reply)
                reply = re.sub(r"\s*```$", "", reply)

            parsed = json.loads(reply)
            if isinstance(parsed, list):
                queries = []
                for item in parsed:
                    if isinstance(item, dict) and "query" in item:
                        q = str(item["query"]).strip()
                        if 5 <= len(q) <= 100:  # 过滤太短或太长的
                            queries.append(q)
                return queries[:3]
            logger.warning(f"doc={doc['id']} 返回非 list 类型: {type(parsed)}")
        except json.JSONDecodeError as e:
            logger.warning(f"doc={doc['id']} JSON 解析失败 (attempt={attempt + 1}): {e}")
            logger.debug(f"原始返回: {reply[:200]}")
        except Exception as e:
            logger.warning(f"doc={doc['id']} 调用失败 (attempt={attempt + 1}): {e}")
            time.sleep(2)

    return []


# =============================================================
# 3. 主流程
# =============================================================
def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    docs = load_all_docs()
    if not docs:
        logger.error("未读取到任何文档，请检查 Qdrant 连接")
        return 1

    eval_set = []
    failed = 0

    for i, doc in enumerate(docs, 1):
        queries = generate_queries_for_doc(doc)
        if not queries:
            failed += 1
            logger.warning(f"[{i}/{len(docs)}] doc={doc['id']} 生成失败")
            continue

        for q in queries:
            eval_set.append({
                "query": q,
                "relevant_doc_id": doc["id"],
                "source": doc["source"],
            })

        if i % 10 == 0 or i == len(docs):
            logger.info(
                f"进度 [{i}/{len(docs)}] "
                f"成功生成 {len(eval_set)} 条查询，失败 {failed} 个文档"
            )

    # 写入
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(eval_set, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info(f"评估集生成完成: {OUTPUT_PATH}")
    logger.info(f"  总文档数: {len(docs)}")
    logger.info(f"  总查询数: {len(eval_set)}")
    logger.info(f"  失败文档: {failed}")
    logger.info(f"  平均每文档: {len(eval_set) / max(len(docs) - failed, 1):.2f} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
