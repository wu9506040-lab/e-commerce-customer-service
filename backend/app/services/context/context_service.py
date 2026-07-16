"""ContextService - 会话上下文 KV 读写（M14）

按 CLAUDE.md §6 + §9.2.2：services/ 编排层，调 models/conversation_context。
被 chat/orchestrator.py 调用，每轮 /chat 启动期 load，done 后 update。

设计：
- 1:1 → conversations.id（conversation_context.conversation_id 是 PK）
- session_id 是会话公开 ID，需通过 conversations 表反查 conversation.id
- best-effort 写：context DB 异常仅 warning，不影响主流程
- 灰度开关：ENABLE_CONTEXT_STORE=False 时短路返默认上下文（不读不写）

§3.3 YAGNI 边界：
- 不做事件流（context_events），需要时 messages JOIN 即可
- 不做多设备同步，单 session 即可
- 不做 TTL 清理，按 conversations.deleted 跟随
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.clients.mysql_client import with_safe_session
from app.core.config import settings
from app.models.conversation import Conversation
from app.models.conversation_context import ConversationContextRow

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """会话上下文（运行时实例，不直接序列化）"""

    session_id: str
    user_id: int
    last_intent: Optional[str] = None
    current_order_no: Optional[str] = None
    flow_state: Optional[str] = None
    resolved_orders: list = field(default_factory=list)
    flow_payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class ContextService:
    """会话上下文服务（DB 读写）"""

    def load(self, session_id: str, user_id: int) -> ConversationContext:
        """加载会话上下文（不存在则返回空 context）。

        灰度开关：ENABLE_CONTEXT_STORE=False 时直接返空 context（短路）。
        DB 异常 best-effort：返空 context（不抛）。
        """
        if not settings.ENABLE_CONTEXT_STORE:
            return ConversationContext(session_id=session_id, user_id=user_id)

        if not session_id or not user_id:
            return ConversationContext(session_id=session_id or "", user_id=user_id or 0)

        try:
            with with_safe_session(commit=False) as db:
                # 1. 反查 conversation.id by session_id
                conv = db.execute(
                    select(Conversation).where(
                        Conversation.session_id == session_id,
                        Conversation.deleted == 0,
                    )
                ).scalar_one_or_none()
                if conv is None:
                    # conversation 行尚未落库（典型：会话开始第一轮）→ 返空 context
                    return ConversationContext(session_id=session_id, user_id=user_id)

                # 2. 查 context 行
                row = db.execute(
                    select(ConversationContextRow).where(
                        ConversationContextRow.conversation_id == conv.id,
                        ConversationContextRow.deleted == 0,
                    )
                ).scalar_one_or_none()

                if row is None:
                    return ConversationContext(session_id=session_id, user_id=user_id)

                return ConversationContext(
                    session_id=session_id,
                    user_id=user_id,
                    last_intent=row.last_intent,
                    current_order_no=row.current_order_no,
                    flow_state=row.flow_state,
                    resolved_orders=list(row.resolved_orders or []),
                    flow_payload=dict(row.flow_payload or {}),
                )
        except Exception as e:
            logger.warning(
                f"context_service.load failed: session={session_id[:12]}..., "
                f"user_id={user_id}, {e}"
            )
            return ConversationContext(session_id=session_id, user_id=user_id)

    def update(
        self,
        session_id: str,
        user_id: int,
        last_intent: Optional[str] = None,
        current_order_no: Optional[str] = None,
        flow_state: Optional[str] = None,
        resolved_orders: Optional[list] = None,
        flow_payload: Optional[dict] = None,
    ) -> bool:
        """部分更新会话上下文（None 字段不覆盖）。

        灰度开关：ENABLE_CONTEXT_STORE=False 时短路返 True（不写）。
        DB 异常 best-effort：返 False（不抛）。
        """
        if not settings.ENABLE_CONTEXT_STORE:
            return True

        if not session_id or not user_id:
            return False

        try:
            with with_safe_session(commit=True) as db:
                # 1. UPSERT conversation（与现有 session_service 一致：lazy 创建）
                # 这里只读不创建（conversations 由 persist_to_mysql 写穿时创建）；
                # 如果 conversation 还没落库，跳过（让下一轮自然写）
                conv = db.execute(
                    select(Conversation).where(
                        Conversation.session_id == session_id,
                        Conversation.deleted == 0,
                    )
                ).scalar_one_or_none()
                if conv is None:
                    logger.debug(
                        f"context_service.update: conversation 尚未落库, "
                        f"session={session_id[:12]}..., user_id={user_id}（放行）"
                    )
                    return False

                # 2. 读现有行（合并 None 字段用）
                row = db.execute(
                    select(ConversationContextRow).where(
                        ConversationContextRow.conversation_id == conv.id,
                        ConversationContextRow.deleted == 0,
                    )
                ).scalar_one_or_none()

                if row is None:
                    # 不存在 → INSERT
                    new_row = ConversationContextRow(
                        conversation_id=conv.id,
                        user_id=user_id,
                        last_intent=last_intent,
                        current_order_no=current_order_no,
                        flow_state=flow_state,
                        resolved_orders=resolved_orders or [],
                        flow_payload=flow_payload or {},
                    )
                    db.add(new_row)
                else:
                    # 存在 → 部分更新（None 不覆盖）
                    if last_intent is not None:
                        row.last_intent = last_intent
                    if current_order_no is not None:
                        row.current_order_no = current_order_no
                    if flow_state is not None:
                        row.flow_state = flow_state
                    if resolved_orders is not None:
                        row.resolved_orders = resolved_orders
                    if flow_payload is not None:
                        row.flow_payload = flow_payload
                return True
        except Exception as e:
            logger.warning(
                f"context_service.update failed: session={session_id[:12]}..., "
                f"user_id={user_id}, {e}"
            )
            return False


# =============================================================
# 工厂入口（依赖倒置 + 单例）
# =============================================================
_service: Optional[ContextService] = None


def get_context_service() -> ContextService:
    """工厂入口。业务模块**只能**通过此函数获取（禁止直接 new）。"""
    global _service
    if _service is None:
        _service = ContextService()
    return _service


def reset_context_service() -> None:
    """测试钩子：重置单例（仅供 test fixtures）。"""
    global _service
    _service = None