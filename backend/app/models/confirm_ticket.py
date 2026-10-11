"""confirm_tickets 表 ORM model - 高风险工具人机协同护栏

V14（human-in-the-loop）落地铁律：
- AI 只负责"填表"——出结构化提案
- "签字确认"由人完成——服务端校验后真执行
- 服务端持票的方式落库：用户前端显示什么，服务端就照那个执行（防"AI 偷偷改参数"）

字段设计要点：
- args_hash：写入时把 tool_args 整体冻结（md5 of JSON-canonical），confirm 时再算一遍，
  不一致即拒绝——这条挡住了"AI 出卡片后偷偷改金额再让用户点确认"的攻击
- status 状态机：proposed → confirmed → executed / rejected / expired
  merged 只能 reviewed → confirmed；自动过期由 has_proposed_task 在 confirm 入口检查
- 全量 timestamp：proposed/confirmed/executed/expired 四事件均可回放审计
"""
import datetime as dt
from typing import Optional

from sqlalchemy import (
    JSON, BigInteger, DateTime, Index, Integer, SmallInteger, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

STATUS_PROPOSED = "proposed"
STATUS_CONFIRMED = "confirmed"
STATUS_EXECUTED = "executed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"


class ConfirmTicket(Base):
    __tablename__ = "confirm_tickets"
    __table_args__ = (
        # 同会话+同工具+同 args_hash 唯一：同一提案不会被 AI 重复出卡片
        # 防"双花"或"AI 自欺欺人重复产出"
        UniqueConstraint("session_id", "tool_name", "args_hash",
                         name="uq_confirm_ticket_dedupe"),
        Index("ix_confirm_status_user", "status", "user_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    # 工具名（如 refund_execute / send_email / create_order）—— 注册表查找执行器
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 工具参数 JSON-canonical——confirm 时按相同规范重算 args_hash 比对
    tool_args: Mapped[dict] = mapped_column(JSON, nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # AI 出的摘要（人眼可读，供前端卡片展示与 operation_log 留痕）
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PROPOSED, index=True)
    # 状态机四事件 timestamp：审计 / 状态卡严格流转的判定字段
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    confirmed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    rejected_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    # 执行结果快照（成功后塞 dict；失败塞 {"error": "..."}）—— 留档可回放
    result_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    create_time: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    update_time: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())
    deleted: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"<ConfirmTicket id={self.id} tool={self.tool_name} "
            f"status={self.status} user={self.user_id}>"
        )