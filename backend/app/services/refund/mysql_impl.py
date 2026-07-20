"""
MySQL 默认实现 — MySQLRefundService（Sprint 16）

实现 services/refund/protocols.py 的 RefundService Protocol。
沿用现有 RefundTool 的查询语义（越权防护 / 软删过滤），
把 ORM 行映射为 DTO（app/schemas/business.py.Refund）。

async 方法内部走 sync `with_safe_session`（clients 层复用），
与 mysql_client 的 sync SDK 风格一致（CLAUDE.md §7.2 clients 只做连接）。
本文件是**唯一**允许 import mysql_client 的 Protocol 实现处（Tool 层改走 Protocol）。
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.clients.mysql_client import with_safe_session
from app.models.order import Order as OrderORM
from app.models.refund import Refund as RefundORM
from app.schemas.business import Refund

logger = logging.getLogger(__name__)


# =============================================================
# ORM → DTO 映射（纯函数）
# =============================================================
def _to_refund(r: RefundORM, order_no: Optional[str] = None) -> Refund:
    return Refund(
        refund_no=r.refund_no,
        order_no=order_no,                    # list / get 注入；status 不注入
        user_id=r.user_id,
        status=r.status,
        amount=float(r.amount),
        reason=r.reason,
        remark=r.remark,
        create_time=r.create_time,
        update_time=r.update_time,
    )


# =============================================================
# MySQLRefundService
# =============================================================
class MySQLRefundService:
    """RefundService Protocol 的 MySQL 默认实现"""

    async def get_refund(self, user_id: int, refund_no: str) -> Optional[Refund]:
        with with_safe_session(commit=False) as db:
            r = db.query(RefundORM).filter(
                RefundORM.refund_no == refund_no,
                RefundORM.user_id == user_id,     # 越权防护
                RefundORM.deleted == 0,
            ).first()
            if not r:
                return None
            order = db.query(OrderORM).filter(OrderORM.id == r.order_id).first()
            return _to_refund(r, order_no=order.order_no if order else None)

    async def list_user_refunds(
        self, user_id: int, status: Optional[str] = None,
        start_date: Optional[datetime] = None, end_date: Optional[datetime] = None,
        limit: int = 20, cursor: Optional[str] = None,
    ) -> tuple[List[Refund], Optional[str]]:
        offset = _decode_cursor(cursor)
        with with_safe_session(commit=False) as db:
            q = db.query(RefundORM).filter(
                RefundORM.user_id == user_id,
                RefundORM.deleted == 0,
            )
            if status:
                q = q.filter(RefundORM.status == status)
            if start_date:
                q = q.filter(RefundORM.create_time >= start_date)
            if end_date:
                q = q.filter(RefundORM.create_time <= end_date)
            q = q.order_by(RefundORM.create_time.desc())
            # 多取 1 条判断是否还有下一页
            rows = q.offset(offset).limit(limit + 1).all()
            has_more = len(rows) > limit
            rows = rows[:limit]

            # 批量拿 order_no（避免 N+1）
            order_ids = [r.order_id for r in rows]
            if order_ids:
                order_no_map = {
                    o.id: o.order_no
                    for o in db.query(OrderORM).filter(OrderORM.id.in_(order_ids)).all()
                }
            else:
                order_no_map = {}

            refunds = [_to_refund(r, order_no=order_no_map.get(r.order_id)) for r in rows]
            next_cursor = _encode_cursor(offset + limit) if has_more else None
            return refunds, next_cursor

    async def get_refund_status(self, refund_no: str) -> Optional[str]:
        with with_safe_session(commit=False) as db:
            r = db.query(RefundORM).filter(
                RefundORM.refund_no == refund_no,
                RefundORM.deleted == 0,
            ).first()
            return r.status if r else None


# =============================================================
# cursor 编解码（offset-based，与 order/mysql_impl.py 一致）
# =============================================================
def _encode_cursor(offset: int) -> str:
    return str(offset)


def _decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except (ValueError, TypeError):
        return 0