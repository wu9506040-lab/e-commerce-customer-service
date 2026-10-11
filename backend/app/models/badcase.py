"""badcase 候选池 ORM model - 对应 badcase_candidates 表

V14（2.4 飞轮机制化）：三类信号自动入池——
- thumbs_down     用户 👎（rate 写入侧实时入池，join 2.3 的 intent_snapshot）
- low_confidence  LLM 意图低置信（collect 扫描 assistant 消息快照，conf < 阈值）
- guard_blocked   Guard 拦截留证（operation_log 不记正文，误杀样本必须显式落池）

状态机：new → reviewed / discarded；reviewed → merged（由 merge_badcase_to_eval.py
人工审核后合入评测集时回写）。(query_hash, signal) 唯一：同问句同信号只 bump
hit_count，不重复建行——复发本身就是信号强度。
"""
import datetime
from typing import Optional

from sqlalchemy import (
    JSON, BigInteger, DateTime, Integer, SmallInteger, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 合法信号与状态（服务层与 API 层共用）
SIGNALS = ("thumbs_down", "low_confidence", "guard_blocked")
STATUS_NEW = "new"
STATUS_REVIEWED = "reviewed"
STATUS_DISCARDED = "discarded"
STATUS_MERGED = "merged"


class BadcaseCandidate(Base):
    __tablename__ = "badcase_candidates"
    __table_args__ = (
        UniqueConstraint("query_hash", "signal_type", name="uq_badcase_signal_query"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    # 列名用 signal_type：`signal` 是 MySQL 8 保留字（SIGNAL 语句），手写 SQL/BI 工具
    # 不带反引号就炸（2026-10-11 建表当日实测踩中）。ORM 属性与 API JSON 键保持 signal 不变。
    signal: Mapped[str] = mapped_column("signal_type", String(16), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, index=True)
    # 当轮用户问句原文（评测集增量就以它为 query 字段）
    query: Mapped[str] = mapped_column(Text, nullable=False)
    query_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    # 信号现场：intent 快照 / top 检索上下文摘要 / guard 层与理由 / 答案节选
    snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_NEW, index=True)
    # 审核标注：本应命中的 KB 源（稳定键，防 doc_id 漂移）；缺省时 merge 脚本兜底取现场首个 source
    expected_source: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    reviewed_by: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    merged_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[datetime.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"<BadcaseCandidate id={self.id} signal={self.signal} "
            f"status={self.status} hits={self.hit_count}>"
        )
