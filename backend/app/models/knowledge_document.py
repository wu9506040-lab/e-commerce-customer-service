"""知识库文档元数据 ORM model - 对应 knowledge_documents 表"""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploader_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
