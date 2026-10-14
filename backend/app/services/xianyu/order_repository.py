"""
XianyuOrderRepository — 闲鱼订单 CRUD + 状态机（V16.3.2）

设计要点：
- upsert_from_tip：幂等（按 (seller_unb, unb, order_no) 去重）
- 状态机：单向升级 + 降级到 refunded
  paid → shipped → signed → received
  paid/shipped/signed/received → refunded（可降级）
  refunded → 任何状态（防回退，保护买家）
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.xianyu_order import XianyuOrder

logger = logging.getLogger(__name__)


# 状态升级序列
_STATE_ORDER = ["paid", "shipped", "signed", "received", "refunded"]


def _is_upgrade(current: str, new: str) -> bool:
    """判断 new 是否比 current 更靠后（升级）"""
    if current not in _STATE_ORDER or new not in _STATE_ORDER:
        return False
    return _STATE_ORDER.index(new) > _STATE_ORDER.index(current)


def _is_downgrade_to_refunded(current: str, new: str) -> bool:
    """判断是否降级到 refunded（任意状态 → refunded）"""
    return new == "refunded" and current != "refunded"


def _is_blocked_by_refunded(current: str, new: str) -> bool:
    """refunded 后任何新状态都拒绝（防攻击）"""
    return current == "refunded" and new != "refunded"


class XianyuOrderRepository:
    """xianyu_orders 表 CRUD + 业务查询"""

    @staticmethod
    def upsert_from_tip(
        db: Session,
        seller_unb: str,
        unb: str,
        item_id: Optional[str],
        order_no: str,
        new_status: str,
        tip_source: str,
        raw_message_id: Optional[str] = None,
        amount: Optional[float] = None,
    ) -> XianyuOrder:
        """根据 tip 落库（同 (seller_unb, unb, order_no) 幂等）

        状态转换规则：
        1. 升级：paid→shipped→signed→received（按时间序）
        2. 降级：任意→refunded（一旦退款，订单就是"已退款"）
        3. 锁定：refunded→其他（拒绝，保护"重复退款检测"）
        """
        existing = (
            db.query(XianyuOrder)
            .filter_by(
                seller_unb=seller_unb,
                unb=unb,
                order_no=order_no,
                deleted=0,
            )
            .first()
        )

        if existing is None:
            # 新建
            order = XianyuOrder(
                seller_unb=seller_unb,
                unb=unb,
                item_id=item_id,
                order_no=order_no,
                amount=amount,
                status=new_status,
                tip_source=tip_source,
                raw_message_id=raw_message_id,
            )
            db.add(order)
            db.flush()
            logger.info(
                f"XianyuOrder 新建 seller={seller_unb} unb={unb} "
                f"order_no={order_no} status={new_status}"
            )
            return order

        # 已存在：判断是否更新
        if _is_blocked_by_refunded(existing.status, new_status):
            logger.warning(
                f"XianyuOrder 已退款，拒绝回退：{existing.status} → {new_status} "
                f"unb={unb} order_no={order_no}"
            )
            return existing

        if _is_upgrade(existing.status, new_status) or _is_downgrade_to_refunded(
            existing.status, new_status
        ):
            existing.status = new_status
            existing.tip_source = tip_source
            if raw_message_id:
                existing.raw_message_id = raw_message_id
            if amount is not None and existing.amount is None:
                existing.amount = amount
            existing.updated_at = datetime.datetime.now()
            db.flush()
            logger.info(
                f"XianyuOrder 更新 {existing.status} → {new_status} "
                f"unb={unb} order_no={order_no}"
            )
        # else: 同状态或回退，no-op

        return existing

    @staticmethod
    def list_by_buyer(
        db: Session, seller_unb: str, unb: str
    ) -> list[XianyuOrder]:
        """查该买家的所有订单（按 updated_at 倒序）"""
        return (
            db.query(XianyuOrder)
            .filter_by(seller_unb=seller_unb, unb=unb, deleted=0)
            .order_by(XianyuOrder.updated_at.desc())
            .all()
        )

    @staticmethod
    def get_latest_refunded(
        db: Session, seller_unb: str, unb: str
    ) -> Optional[XianyuOrder]:
        """查该买家最近一笔已退款订单（**重复退款检测**）"""
        return (
            db.query(XianyuOrder)
            .filter_by(
                seller_unb=seller_unb, unb=unb, status="refunded", deleted=0
            )
            .order_by(XianyuOrder.updated_at.desc())
            .first()
        )

    @staticmethod
    def get_active_order(
        db: Session, seller_unb: str, unb: str
    ) -> Optional[XianyuOrder]:
        """查该买家最近一笔非退款订单（用于"未退款的订单"决策）"""
        return (
            db.query(XianyuOrder)
            .filter(
                XianyuOrder.seller_unb == seller_unb,
                XianyuOrder.unb == unb,
                XianyuOrder.status != "refunded",
                XianyuOrder.deleted == 0,
            )
            .order_by(XianyuOrder.updated_at.desc())
            .first()
        )


__all__ = ["XianyuOrderRepository"]