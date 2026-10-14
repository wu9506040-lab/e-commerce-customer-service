
"""闲鱼独立表 ORM（V16.3.3）"""
import datetime
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, JSON, Numeric, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class XianyuBuyerProfile(Base):
    """闲鱼买家画像（7 天 TTL）"""
    __tablename__ = "xianyu_buyer_profiles"
    
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    unb: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    nick: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_returning_buyer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_message_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    last_query_topic: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    conversation_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=datetime.datetime.now)


class XianyuConversation(Base):
    """闲鱼会话消息（含 tip 事件）"""
    __tablename__ = "xianyu_conversations"
    
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    unb: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cid: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # buyer / seller / system
    message_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_tip: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tip_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # paid/shipped/signed/refunded
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    
    __table_args__ = (
        Index("idx_unb_cid_time", "unb", "cid", "created_at"),
    )


class XianyuItem(Base):
    """闲鱼商品话术（item_id → 专属话术）"""
    __tablename__ = "xianyu_items"
    
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    item_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extra: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=datetime.datetime.now)
