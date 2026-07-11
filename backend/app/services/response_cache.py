"""
Response Cache - 重复 query 不再调 LLM

按 CLAUDE.md §5 Scope Lock：services/ 做业务编排
被 app/api/chat.py 在 Synthesizer 之前调用

两层缓存：
  L1 Exact match：md5(query) 命中即返上次 answer（最快，0 token）
  L2 Semantic match：embedding 相似度 > 0.95 命中（保 paraphrase 也命中）

设计：
- per-user 缓存（不同用户不复用）
- TTL 10 分钟（10min 内重复 → 命中；过期 → 重算）
- 写入时机：Synthesizer.run_stream 完成后（chat.py 的 done 事件里调 cache.put）
- 读时机：chat.py 在 guard.check 之后、Synthesizer.run_stream 之前
- 降级：Redis 挂了 / embedding 失败 → 静默放行（不误伤）

存储：
- L1 key: rcache:exact:{user_id}:{md5(query)}
- L1 value: 完整 answer 文本
- L2 key: rcache:sem:{user_id}:{md5(query)}
- L2 value: JSON {"answer": str, "embedding": [float x 1024]}
"""
import hashlib
import json
import logging
import time
from typing import Optional

from app.clients.redis_client import get_client as redis_get
from app.core.providers.embedding import EmbeddingError, get_embedding_provider

logger = logging.getLogger(__name__)

# TTL：10 分钟（10min 内同一问题复用）
CACHE_TTL_SECONDS = 600
# Semantic 相似度阈值（0.95 = 几乎同义）
SEMANTIC_THRESHOLD = 0.95
# 最多存的语义缓存条数（per user），防爆内存
MAX_SEMANTIC_ENTRIES_PER_USER = 50

_EXACT_KEY_PREFIX = "rcache:exact:"
_SEM_KEY_PREFIX = "rcache:sem:"
_SEM_INDEX_PREFIX = "rcache:sem_idx:"  # 维护每用户的 sem key 列表


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# =============================================================
# L1 Exact Match
# =============================================================
def get_exact(query: str, user_id: int) -> Optional[str]:
    """Exact match：md5 命中返 answer；未命中 / 异常返 None"""
    if user_id <= 0 or not query:
        return None
    try:
        r = redis_get()
        key = f"{_EXACT_KEY_PREFIX}{user_id}:{_md5(query)}"
        return r.get(key)
    except Exception as e:
        logger.warning(f"[rcache] exact get 异常（放行）: {e}")
        return None


def put_exact(query: str, user_id: int, answer: str) -> None:
    """存 exact 缓存（best-effort）"""
    if user_id <= 0 or not query or not answer:
        return
    try:
        r = redis_get()
        key = f"{_EXACT_KEY_PREFIX}{user_id}:{_md5(query)}"
        r.setex(key, CACHE_TTL_SECONDS, answer[:8000])  # 截断 8k 字符
    except Exception as e:
        logger.warning(f"[rcache] exact put 异常: {e}")


# =============================================================
# L2 Semantic Match
# =============================================================
def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot  # a/b 假设已 L2 normalized


def _scan_semantic_keys(user_id: int) -> list[str]:
    """扫该用户所有 sem cache keys（用 index set 维护，避免 KEYS 全扫）"""
    try:
        r = redis_get()
        idx_key = f"{_SEM_INDEX_PREFIX}{user_id}"
        # 返回 set 里所有 key 名字
        members = r.smembers(idx_key)
        return [f"{_SEM_KEY_PREFIX}{user_id}:{m}" for m in members]
    except Exception as e:
        logger.warning(f"[rcache] sem scan 异常: {e}")
        return []


def get_semantic(query: str, user_id: int) -> Optional[str]:
    """Semantic match：embedding 相似度 > 阈值命中返 answer"""
    if user_id <= 0 or not query:
        return None
    try:
        # 1. 算 query embedding
        try:
            q_emb = get_embedding_provider().embed_text(query)
        except EmbeddingError as e:
            logger.warning(f"[rcache] sem embed 失败（放行）: {e}")
            return None

        # 2. 扫该用户所有 sem cache
        keys = _scan_semantic_keys(user_id)
        if not keys:
            return None

        r = redis_get()
        # 3. 算每条的 cosine，取最高
        best_sim = 0.0
        best_answer: Optional[str] = None
        for k in keys:
            raw = r.get(k)
            if not raw:
                # 清理 index set（条目已过期）
                r.srem(f"{_SEM_INDEX_PREFIX}{user_id}", k.split(":")[-1])
                continue
            try:
                entry = json.loads(raw)
                cached_emb = entry.get("embedding")
                cached_answer = entry.get("answer")
                if not cached_emb or not cached_answer:
                    continue
                sim = _cosine(q_emb, cached_emb)
                if sim > best_sim:
                    best_sim = sim
                    best_answer = cached_answer
            except (json.JSONDecodeError, TypeError):
                continue

        if best_sim >= SEMANTIC_THRESHOLD and best_answer is not None:
            logger.info(
                f"[rcache] sem 命中: user={user_id} sim={best_sim:.3f} "
                f"query={query[:30]!r}"
            )
            return best_answer[:8000]
        return None
    except Exception as e:
        logger.warning(f"[rcache] sem get 异常（放行）: {e}")
        return None


def put_semantic(query: str, user_id: int, answer: str) -> None:
    """存 semantic 缓存（算 embedding + Redis）"""
    if user_id <= 0 or not query or not answer:
        return
    try:
        try:
            q_emb = get_embedding_provider().embed_text(query)
        except EmbeddingError as e:
            logger.warning(f"[rcache] sem put embed 失败: {e}")
            return

        r = redis_get()
        md5 = _md5(query)
        entry_key = f"{_SEM_KEY_PREFIX}{user_id}:{md5}"
        idx_key = f"{_SEM_INDEX_PREFIX}{user_id}"

        # 存 entry
        payload = json.dumps({
            "query": query[:500],
            "answer": answer[:8000],
            "embedding": q_emb,
            "create_ts": int(time.time()),
        })
        pipe = r.pipeline()
        pipe.setex(entry_key, CACHE_TTL_SECONDS, payload)
        pipe.sadd(idx_key, md5)
        # index set 自己设个更长的 TTL（entry TTL 短，index 跟着 entry 走）
        pipe.expire(idx_key, CACHE_TTL_SECONDS * 2)
        pipe.execute()

        # LRU 截断（per user 最多 N 条）
        size = r.scard(idx_key)
        if size > MAX_SEMANTIC_ENTRIES_PER_USER:
            # 简单策略：随机删一批（实际可用 sorted set + ts 淘汰）
            excess = int(size) - MAX_SEMANTIC_ENTRIES_PER_USER
            members = list(r.smembers(idx_key))[:excess]
            if members:
                pipe = r.pipeline()
                for m in members:
                    pipe.delete(f"{_SEM_KEY_PREFIX}{user_id}:{m}")
                    pipe.srem(idx_key, m)
                pipe.execute()
    except Exception as e:
        logger.warning(f"[rcache] sem put 异常: {e}")


# =============================================================
# 统一接口
# =============================================================
def get_cached_answer(query: str, user_id: int) -> Optional[str]:
    """优先 exact → semantic → None"""
    ans = get_exact(query, user_id)
    if ans is not None:
        logger.info(f"[rcache] exact 命中: user={user_id} query={query[:30]!r}")
        return ans
    return get_semantic(query, user_id)


def put_cached_answer(query: str, user_id: int, answer: str) -> None:
    """存两层（best-effort）"""
    put_exact(query, user_id, answer)
    put_semantic(query, user_id, answer)
