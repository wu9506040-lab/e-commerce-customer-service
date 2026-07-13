"""
退款 Tool - 纯 DB 查询 + 可退规则判断

按 PROJECT_DESIGN.md §3：refund_query 复合路径 = tool（订单状态）+ policy RAG。
本文件只做 tool 部分；policy RAG 在 services/refund_service.py 编排。
"""
import datetime
from typing import Optional

from sqlalchemy import select

from app.clients.mysql_client import with_safe_session
from app.models.order import Order, OrderStatus
from app.models.refund import Refund
from app.services.config_loader import get_config_loader

# 业务规则（启动期加载一次，来自 config/business_rules/refund.yaml）
# 单一真相源：与 refund_graph / order_lifecycle 共享同一份 YAML
_REFUND_RULES = get_config_loader().load("refund")


class RefundTool:
    """退款查询工具"""

    # 7 天无理由：已签收后超过 7 天不可退（来自 refund.yaml）
    REFUND_WINDOW_DAYS: int = _REFUND_RULES["REFUND_WINDOW_DAYS"]

    @staticmethod
    def list_user_refunds(user_id: int, limit: int = 20) -> list[dict]:
        """查某用户的所有退款记录"""
        with with_safe_session(commit=False) as db:
            refunds = db.query(Refund).filter(
                Refund.user_id == user_id,
                Refund.deleted == 0,
            ).order_by(Refund.create_time.desc()).limit(limit).all()

            # 批量拿 order_no（避免 N+1）
            order_ids = [r.order_id for r in refunds]
            if order_ids:
                order_no_map = {
                    o.id: o.order_no
                    for o in db.query(Order).filter(Order.id.in_(order_ids)).all()
                }
            else:
                order_no_map = {}

            return [
                {
                    **RefundTool._to_dict(r),
                    "order_no": order_no_map.get(r.order_id),
                }
                for r in refunds
            ]

    @staticmethod
    def get_refund_by_no(user_id: int, refund_no: str) -> Optional[dict]:
        """按退款号查（强制 user_id 防越权）"""
        with with_safe_session(commit=False) as db:
            refund = db.query(Refund).filter(
                Refund.refund_no == refund_no,
                Refund.user_id == user_id,
                Refund.deleted == 0,
            ).first()
            if not refund:
                return None
            order = db.query(Order).filter(Order.id == refund.order_id).first()
            return {
                **RefundTool._to_dict(refund),
                "order_no": order.order_no if order else None,
            }

    @staticmethod
    def check_refundable(user_id: int, order_no: str) -> dict:
        """
        判断某订单能否退款（单步规则，不调 LLM）

        规则（V2 简化版）：
        - 订单不存在 / 不属于该 user → 不可退
        - 订单已是 refunded 状态 → 不可退
        - 已签收(delivered)：7 天内可退，超期不可退
        - 其他状态：都可发起退款申请

        Returns:
            {
                "refundable": bool,
                "reason": str,           # 中文提示
                "order_no": str,
                "order_status": str,
                "days_since_order": int, # 用于 policy 融合时引用
            }
        """
        with with_safe_session(commit=False) as db:
            order = db.query(Order).filter(
                Order.order_no == order_no,
                Order.user_id == user_id,  # 越权防护
                Order.deleted == 0,
            ).first()

            if not order:
                return {
                    "refundable": False,
                    "reason": "订单不存在或不属于当前用户",
                    "order_no": order_no,
                    "order_status": None,
                    "days_since_order": None,
                }

            days = (datetime.datetime.now() - order.create_time).days if order.create_time else 0

            if order.status == OrderStatus.REFUNDED.value:
                return {
                    "refundable": False,
                    "reason": "该订单已退款，无法重复申请",
                    "order_no": order_no,
                    "order_status": order.status,
                    "days_since_order": days,
                }

            if order.status == OrderStatus.DELIVERED.value:
                if days > RefundTool.REFUND_WINDOW_DAYS:
                    return {
                        "refundable": False,
                        "reason": f"已签收 {days} 天，超过 {RefundTool.REFUND_WINDOW_DAYS} 天无理由退货期限",
                        "order_no": order_no,
                        "order_status": order.status,
                        "days_since_order": days,
                    }
                return {
                    "refundable": True,
                    "reason": f"已签收 {days} 天，在 {RefundTool.REFUND_WINDOW_DAYS} 天无理由退货期限内",
                    "order_no": order_no,
                    "order_status": order.status,
                    "days_since_order": days,
                }

            # pending / paid / shipped / completed：都可发起
            return {
                "refundable": True,
                "reason": f"订单状态「{order.status}」，可发起退款申请",
                "order_no": order_no,
                "order_status": order.status,
                "days_since_order": days,
            }

    @staticmethod
    def _to_dict(r: Refund) -> dict:
        return {
            "refund_no": r.refund_no,
            "reason": r.reason,
            "status": r.status,
            "amount": float(r.amount),
            "create_time": r.create_time.isoformat() if r.create_time else None,
        }