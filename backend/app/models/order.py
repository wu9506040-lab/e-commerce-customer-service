"""订单 ORM model - 对应 orders + order_items 表

强耦合（订单和明细生命周期一致），同文件维护。
状态机见 OrderStatus，业务层必须用枚举值，禁止字符串硬编码。
"""
import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrderStatus(str, Enum):
    """订单状态机（业务层强约束）"""
    PENDING = "pending"      # 待支付
    PAID = "paid"            # 已支付
    SHIPPED = "shipped"      # 已发货
    DELIVERED = "delivered"  # 已签收
    COMPLETED = "completed"  # 已完成
    REFUNDED = "refunded"    # 已退款（终态）


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OrderStatus.PENDING.value)
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    address_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # L1 增量（Sprint 18-C · 售中地址修改）：冗余字符串地址（V2 售中协议使用）。
    # 早期只有 address_id 整数外键；售中改地址需明文字段给客服/用户看，故新增。
    shipping_address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    create_time: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Order id={self.id} order_no={self.order_no} status={self.status}>"


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 冗余字段：商品改名/下架后，订单历史仍可读
    sku: Mapped[str] = mapped_column(String(50), nullable=False)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    create_time: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<OrderItem id={self.id} sku={self.sku} qty={self.qty}>"