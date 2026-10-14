"""
XianyuOrder ORM model - 闲鱼订单表（V16.3 新增）

落库来源：从 message history 的 tip.tip 解析
用途：智能客服查订单状态、识别"重复退款"、统计销售
"""
import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, SmallInteger, String, Text, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class XianyuOrder(Base):
    __tablename__ = "xianyu_orders"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    unb: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    seller_unb: Mapped[str] = mapped_column(String(32), nullable=False)
    item_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    order_no: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    # 5 态：paid / shipped / signed / received / refunded
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="paid")
    tip_source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("seller_unb", "unb", "order_no", name="uk_seller_unb_order"),
        Index("idx_seller_unb_status", "seller_unb", "unb", "status"),
    )

    def __repr__(self) -> str:
        return f"<XianyuOrder id={self.id} unb={self.unb} order_no={self.order_no} status={self.status}>"
