"""
MySQL 冷路径 - 会话元数据与消息持久化（§11 write-through）

按 §6 规则：services/ 编排层，调 clients/mysql_client。
被 services/session_service.py 组合使用。

设计：
- 仅管 MySQL 冷路径（写穿 + 回填），不碰 Redis
- 所有 session/transaction 走 with_safe_session（Stage 1 抽取）
- 失败仅 warning，不抛（MySQL 是冷路径，挂掉不能影响 /chat 热路径）
"""
import logging
from typing import Dict, List, Optional

from sqlalchemy import func, select

from app.clients.mysql_client import with_safe_session
from app.models.conversation import Conversation
from app.models.message import Message

logger = logging.getLogger(__name__)

# =============================================================
# 读（Redis miss 时回填用）
# =============================================================
def load_history_mysql(session_id: str, limit: int = 20) -> List[Dict]:
    """
    从 MySQL 读历史（Redis miss 时的回填源）
    按 create_time DESC 取最近 N 条，反转为正序返回。
    失败仅 warning，返回空列表（与 with_safe_session 行为一致）。
    """
    history: List[Dict] = []
    with with_safe_session(commit=False) as db:
        rows = db.execute(
            select(Message)
            .where(Message.session_id == session_id, Message.deleted == 0)
            .order_by(Message.create_time.desc())
            .limit(limit)
        ).scalars().all()

        if rows:
            history = [
                {
                    "role": r.role,
                    "content": r.content,
                    "ts": int(r.create_time.timestamp()),
                }
                for r in reversed(rows)
            ]
            logger.info(
                f"mysql_store.load_history_mysql: session={session_id[:12]}..., "
                f"loaded={len(history)}"
            )

    return history


# =============================================================
# 写（write-through §11）
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

    Args:
        session_id: 会话 ID
        user_id: 用户 ID（0 = 匿名）
        user_content: 用户消息
        assistant_content: 助手消息
        contexts: RAG 检索的 context 列表（仅 assistant 行存）
        scores: 对应相似度（仅 assistant 行存）
        latency_ms: LLM 响应耗时（仅 assistant 行存）
        token_count: LLM token 数（仅 assistant 行存）
    """
    if not session_id or not user_content or not assistant_content:
        logger.warning(
            f"mysql_store.persist_to_mysql: 参数不完整，跳过 "
            f"session={session_id[:12] if session_id else 'None'}..."
        )
        return

    # with_safe_session 内部 commit + 异常吞咽 + warning
    with with_safe_session(commit=True) as db:
        # 1. UPSERT conversation
        existing = db.execute(
            select(Conversation).where(
                Conversation.session_id == session_id,
                Conversation.deleted == 0,
            )
        ).scalar_one_or_none()

        if existing is None:
            # 新会话
            conv = Conversation(
                session_id=session_id,
                user_id=user_id,
                title=user_content[:200] if user_content else None,
                status=1,
                message_count=2,  # 本轮 user + assistant
                first_query=user_content[:500] if user_content else None,
                last_message_at=func.now(),
            )
            db.add(conv)
        else:
            # 续聊
            existing.message_count = (existing.message_count or 0) + 2
            existing.last_message_at = func.now()

        # 2. INSERT messages（2 行：user + assistant）
        db.add_all([
            Message(
                session_id=session_id,
                user_id=user_id,
                role="user",
                content=user_content,
            ),
            Message(
                session_id=session_id,
                user_id=user_id,
                role="assistant",
                content=assistant_content,
                contexts=contexts,
                scores=scores,
                token_count=token_count,
                latency_ms=latency_ms,
            ),
        ])

        logger.debug(
            f"mysql_store.persist_to_mysql: session={session_id[:12]}..., "
            f"user_id={user_id}, messages_added=2"
        )
