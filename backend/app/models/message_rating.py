"""回答评价 ORM model - 对应 message_ratings 表

WP1 业务指标体系：CSAT（满意度）的数据源。
用户对某条 assistant 回答 👍/，(session_id, message_id, user_id) 唯一，
重复评价按更新处理（kpi_service.upsert_rating）。
"""
import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MessageRating(Base):
    __tablename__ = "message_ratings"
    __table_args__ = (
        UniqueConstraint("session_id", "message_id", "user_id", name="uq_rating_target"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # 无消息 id 时记 0（按会话粒度）
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    rating: Mapped[str] = mapped_column(String(8), nullable=False)  # up | down
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<MessageRating session={self.session_id} rating={self.rating}>"
