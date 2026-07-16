"""会话上下文 ORM model - 对应 conversation_contexts 表（M14）

1:1 → conversations.id；存 session 级 KV 状态：
- last_intent / current_order_no / flow_state / resolved_orders / flow_payload

L1 级别新增表（不破坏 conversations schema）；灰度 ENABLE_CONTEXT_STORE=False 时不读不写。
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    JSON,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConversationContextRow(Base):
    __tablename__ = "conversation_contexts"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True,
        comment="所属会话 ID（逻辑外键 → conversations.id）",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True,
        comment="所属用户 ID（冗余便于查询）",
    )
    last_intent: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, comment="上一轮意图",
    )
    current_order_no: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="当前会话锁定的订单号",
    )
    flow_state: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        comment="业务流状态: refund.completed / logistics.tracking",
    )
    resolved_orders: Mapped[Optional[list]] = mapped_column(
        JSON, nullable=True,
        comment="Resolver 推断过的订单列表 [{order_no, status, picked_at}]",
    )
    flow_payload: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True, comment="业务流中间态（dict）",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0,
    )

    def __repr__(self) -> str:
        return (
            f"<ConversationContextRow conv_id={self.conversation_id} "
            f"intent={self.last_intent} order={self.current_order_no}>"
        )