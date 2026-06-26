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
from typing import Dict, List

from app.clients.redis_client import get_client as redis_get

logger = logging.getLogger(__name__)

# =============================================================
# 配置
# =============================================================
KEY_PREFIX = "chat:session:"
SESSION_TTL_SECONDS = 24 * 3600  # 24 小时
MAX_HISTORY = 20                  # 单会话最多保留多少条（防止内存爆）
ANONYMOUS_USER_ID = 0             # 匿名用户约定 ID（schema user_id NOT NULL）


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
