"""
Session Service - 会话门面（V1 收口 §15.3）

按 §6 规则：services/ 编排层，组合 redis_store + mysql_store。
对外暴露与原 chat_history.py 完全一致的 API，api/ 层无感切换。

设计：
- 门面模式：上层（api/）只调本模块，不直接调 redis_store / mysql_store
- Redis miss → MySQL 回填（load_history_with_fallback 编排两 store）
- ANONYMOUS_USER_ID 常量重导出（保持原 chat_history 公开 API）
- 写路径（append_exchange / persist_to_mysql）仍走 best-effort
"""
import logging
from typing import Dict, List, Optional

from app.services import mysql_store, redis_store

logger = logging.getLogger(__name__)

# =============================================================
# 重导出常量（保持与原 chat_history.py 公开 API 完全一致）
# =============================================================
ANONYMOUS_USER_ID = redis_store.ANONYMOUS_USER_ID  # = 0

# 重导出 redis_store 的关键限制（供其他模块引用，避免散落硬编码）
MAX_HISTORY = redis_store.MAX_HISTORY


# =============================================================
# 会话标识
# =============================================================
def generate_session_id() -> str:
    """生成新 session_id（uuid4 hex 无连字符）"""
    return redis_store.generate_session_id()


# =============================================================
# 查询（Redis 热路径 + MySQL 回填）
# =============================================================
def load_history(session_id: str, limit: int = MAX_HISTORY) -> List[Dict]:
    """仅查 Redis 热路径（不级联到 MySQL）"""
    return redis_store.load_history(session_id, limit)


def load_history_with_fallback(
    session_id: str, limit: int = MAX_HISTORY
) -> List[Dict]:
    """
    加载历史（Redis miss 时从 MySQL 回填）

    流程：
        1. 先查 Redis（热路径）
        2. 有就返回
        3. 没有 → 查 MySQL，按 create_time DESC 取最近 N 条，反转为正序
    """
    history = redis_store.load_history(session_id, limit)
    if history:
        return history

    # Redis miss → MySQL 回填
    history = mysql_store.load_history_mysql(session_id, limit)
    if history:
        logger.info(
            f"session_service.load_history_with_fallback: redis miss → mysql hit, "
            f"session={session_id[:12]}..., loaded={len(history)}"
        )
    return history


# =============================================================
# 写入（Redis 热路径）
# =============================================================
def append_message(session_id: str, role: str, content: str) -> None:
    """追加 1 条消息到 Redis（仅热路径，MySQL 写穿由调用方负责）"""
    redis_store.append_message(session_id, role, content)


def append_exchange(
    session_id: str, user_content: str, assistant_content: str
) -> None:
    """追加一轮问答到 Redis（仅 user + assistant 两条）"""
    redis_store.append_exchange(session_id, user_content, assistant_content)


# =============================================================
# 写入（MySQL 冷路径，write-through §11）
# =============================================================
def persist_to_mysql(
    session_id: str,
    user_id: int,
    user_content: str,
    assistant_content: str,
    contexts: Optional[List[str]] = None,
    scores: Optional[List[float]] = None,
    latency_ms: Optional[int] = None,
    token_count: Optional[int] = None,
) -> None:
    """
    写穿：把一轮问答写入 MySQL（messages + UPSERT conversations）
    失败仅 warning，不抛（MySQL 是冷路径，挂掉不能影响 /chat 热路径）
    """
    mysql_store.persist_to_mysql(
        session_id=session_id,
        user_id=user_id,
        user_content=user_content,
        assistant_content=assistant_content,
        contexts=contexts,
        scores=scores,
        latency_ms=latency_ms,
        token_count=token_count,
    )


# =============================================================
# 会话管理
# =============================================================
def clear_history(session_id: str) -> bool:
    """清空指定会话（仅 Redis，MySQL 保留作历史）"""
    return redis_store.clear_history(session_id)


def session_exists(session_id: str) -> bool:
    """判断 Redis session 是否存在"""
    return redis_store.session_exists(session_id)
