"""退款 ORM model - 对应 refunds 表"""
import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RefundStatus(str, Enum):
    """退款状态机"""
    PENDING = "pending"        # 待审核
    APPROVED = "approved"      # 已批准
    REJECTED = "rejected"      # 已拒绝
    COMPLETED = "completed"    # 已完成（钱已退）


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    refund_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RefundStatus.PENDING.value)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    remark: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    create_time: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Refund id={self.id} refund_no={self.refund_no} status={self.status}>"