"""转人工工单 ORM model - 对应 handoff_tickets 表

M15 人工介入闭环：此前 EscalationService 只产 payload + 审计日志，
转人工"转"了但没人能接——本表把 handoff 持久化成可排队、可认领、
可回复、可结单的工单对象，admin 坐席工作台围绕它工作。

状态机：pending（待接）→ taken（已认领）→ resolved（人工已回复）→ closed（归档）
resolve 时人工回复以 role=assistant + contexts.human_agent_ticket 注入会话
messages（人和 AI 共用一条对话时间线，后续 AI 也能看到人工答过什么）。
"""
import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, BigInteger, DateTime, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class HandoffStatus(str, Enum):
    """工单状态机"""
    PENDING = "pending"      # 待人工接
    TAKEN = "taken"          # 已认领
    RESOLVED = "resolved"    # 人工已回复并结单
    CLOSED = "closed"        # 关闭（超时/撤销归档）


class HandoffTicket(Base):
    __tablename__ = "handoff_tickets"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)          # EscalationReason 值
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="P2")  # P0/P1/P2
    matched_keyword: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=HandoffStatus.PENDING.value, index=True)
    assignee: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)      # 认领坐席 username
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)            # HandoffPayload 快照（用户名片/最近订单/对话尾/触发上下文）
    reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)               # 人工最终回复内容
    create_time: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<HandoffTicket no={self.ticket_no} status={self.status} priority={self.priority}>"
