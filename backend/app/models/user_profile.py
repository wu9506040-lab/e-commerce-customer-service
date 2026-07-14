"""用户长程记忆 ORM model - 对应 user_profiles 表

P2 长程记忆：1:1 → users.id，存跨 session 用户画像
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, JSON, SmallInteger, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    frequent_skus: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    preferences: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    interaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_active_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<UserProfile user_id={self.user_id} interactions={self.interaction_count}>"
