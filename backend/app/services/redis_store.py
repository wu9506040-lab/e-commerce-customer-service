"""
Redis 热路径 - 会话历史存储（24h TTL，最新 20 条）

按 §6 规则：services/ 编排层，调 clients/redis_client。
被 services/session_service.py 组合使用。
被 api/conversations.py 在 DELETE 时清理缓存。

设计：
- 仅管 Redis 热路径，不写 MySQL（写穿由 session_service / mysql_store 负责）
- append_message 用 pipeline（LPUSH + LTRIM + EXPIRE）一次 RTT
- 失败由调用方处理（best-effort 模式：try/except + warning）
"""
import json
import logging
import time
import uuid
from typing import Dict, List, Optional

from app.clients.redis_client import get_client as redis_get

logger = logging.getLogger(__name__)

# =============================================================
# 配置
# =============================================================
KEY_PREFIX = "chat:session:"
SESSION_TTL_SECONDS = 24 * 3600  # 24 小时
MAX_HISTORY = 20                  # 单会话最多保留多少条（防止内存爆）
ANONYMOUS_USER_ID = 0             # 匿名用户约定 ID（schema user_id NOT NULL）

# =============================================================
# SSE 流式 checkpoint（Sprint P2 / SSE Resume）
# =============================================================
# 设计：每个流式回合分配 stream_id（uuid4().hex[:12]），
#       断流时把 (prefix_text, last_event_id) 写到 Redis HSET，
#       TTL 600s 内可 resume（前端静默重连）。
#       key 格式：chat:stream:{session_id}:{stream_id}
#       resume 次数：chat:stream:resume_count:{session_id}:{stream_id}（独立 key，INCR）
STREAM_KEY_PREFIX = "chat:stream:"
STREAM_RESUME_COUNT_PREFIX = "chat:stream:resume_count:"
STREAM_CHECKPOINT_TTL = 600       # 10 分钟（覆盖普通网络抖动 + 重连时间）
STREAM_RESUME_COUNT_TTL = 600     # 同上
STREAM_MAX_RESUME_TIMES = 2       # 同 stream_id 最多 resume 2 次


def _stream_key(session_id: str, stream_id: str) -> str:
    return f"{STREAM_KEY_PREFIX}{session_id}:{stream_id}"


def _resume_count_key(session_id: str, stream_id: str) -> str:
    return f"{STREAM_RESUME_COUNT_PREFIX}{session_id}:{stream_id}"


def set_stream_checkpoint(
    session_id: str,
    stream_id: str,
    prefix_text: str,
    last_event_id: int,
    query: str,
) -> None:
    """SSE 流式 checkpoint 写入（HSET + EXPIRE）

    每个 token event 后调用（异步 fire-and-forget），断流时 fallback 同步调用。
    """
    r = redis_get()
    key = _stream_key(session_id, stream_id)
    pipe = r.pipeline()
    pipe.hset(
        key,
        mapping={
            "prefix_text": prefix_text,
            "last_event_id": str(last_event_id),
            "query": query,
            "created_at": str(int(time.time())),
        },
    )
    pipe.expire(key, STREAM_CHECKPOINT_TTL)
    pipe.execute()


def get_stream_checkpoint(
    session_id: str, stream_id: str
) -> Optional[Dict[str, str]]:
    """读 SSE 流式 checkpoint；不存在返回 None"""
    r = redis_get()
    raw = r.hgetall(_stream_key(session_id, stream_id))
    if not raw:
        return None
    return {
        "prefix_text": raw.get("prefix_text", ""),
        "last_event_id": raw.get("last_event_id", "0"),
        "query": raw.get("query", ""),
        "created_at": raw.get("created_at", "0"),
    }


def del_stream_checkpoint(session_id: str, stream_id: str) -> None:
    """SSE 流式完成（done）后清理 checkpoint + resume 计数"""
    r = redis_get()
    pipe = r.pipeline()
    pipe.delete(_stream_key(session_id, stream_id))
    pipe.delete(_resume_count_key(session_id, stream_id))
    pipe.execute()


def increment_resume_count(session_id: str, stream_id: str) -> int:
    """累加 resume 次数；返回累加后的值（用于限流 STREAM_MAX_RESUME_TIMES）。

    第一次 INCR 会同时设置 TTL（避免计数 key 永驻）。
    """
    r = redis_get()
    key = _resume_count_key(session_id, stream_id)
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, STREAM_RESUME_COUNT_TTL)
    results = pipe.execute()
    return int(results[0])


def _key(session_id: str) -> str:
    return f"{KEY_PREFIX}{session_id}"


# =============================================================
# 查询
# =============================================================
def load_history(session_id: str, limit: int = MAX_HISTORY) -> List[Dict]:
    """
    仅查 Redis（热路径，不回填）
    返回按时间正序（旧 → 新），便于拼接 prompt。
    """
    if not session_id:
        return []

    r = redis_get()
    raw = r.lrange(_key(session_id), 0, limit - 1)
    if not raw:
        return []

    history: List[Dict] = []
    for item in reversed(raw):
        try:
            history.append(json.loads(item))
        except json.JSONDecodeError:
            logger.warning(f"history 解析失败，跳过: {item[:80]}")
            continue

    logger.info(
        f"redis_store.load_history: session={session_id[:12]}..., loaded={len(history)}"
    )
    return history


# =============================================================
# 写入
# =============================================================
def append_message(session_id: str, role: str, content: str) -> None:
    """追加 1 条消息到 Redis（仅热路径，调用方负责 MySQL 写穿）"""
    if not session_id:
        raise ValueError("append_message: session_id 不能为空")
    if role not in ("user", "assistant"):
        raise ValueError(
            f"append_message: role 必须是 'user' 或 'assistant'，收到 {role!r}"
        )

    msg = {
        "role": role,
        "content": content,
        "ts": int(time.time()),
    }

    r = redis_get()
    key = _key(session_id)
    pipe = r.pipeline()
    pipe.lpush(key, json.dumps(msg, ensure_ascii=False))
    pipe.ltrim(key, 0, MAX_HISTORY - 1)   # 限长
    pipe.expire(key, SESSION_TTL_SECONDS) # 续 TTL
    pipe.execute()

    logger.debug(
        f"redis_store.append_message: session={session_id[:12]}..., role={role}, "
        f"content_len={len(content)}"
    )


def append_exchange(session_id: str, user_content: str, assistant_content: str) -> None:
    """追加一轮问答到 Redis（仅 user + assistant 两条）"""
    append_message(session_id, "user", user_content)
    append_message(session_id, "assistant", assistant_content)


# =============================================================
# 会话管理
# =============================================================
def clear_history(session_id: str) -> bool:
    """清空指定会话（仅 Redis，MySQL 保留作历史）"""
    if not session_id:
        return False
    deleted = redis_get().delete(_key(session_id))
    logger.info(
        f"redis_store.clear_history: session={session_id[:12]}..., deleted={deleted}"
    )
    return deleted > 0


def session_exists(session_id: str) -> bool:
    """判断 Redis session 是否存在"""
    if not session_id:
        return False
    return redis_get().exists(_key(session_id)) > 0


def generate_session_id() -> str:
    """生成新 session_id（uuid4 hex 无连字符）"""
    return uuid.uuid4().hex
