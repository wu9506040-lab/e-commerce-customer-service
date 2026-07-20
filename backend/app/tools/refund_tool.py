"""
退款 Tool - 可退规则判断 + 退款查询（走 RefundService Protocol）

按 PROJECT_DESIGN.md §3：refund_query 复合路径 = tool（订单状态）+ policy RAG。
本文件只做 tool 部分；policy RAG 在 services/refund_service.py 编排。
"""
import datetime
from typing import Optional

from app.models.order import OrderStatus
from app.services.config_loader import get_config_loader
from app.services.order.factory import get_order_service_factory, run_sync
from app.services.refund.factory import get_refund_service_factory

# 业务规则（启动期加载一次，来自 config/business_rules/refund.yaml）
# 单一真相源：与 refund_graph / order_lifecycle 共享同一份 YAML
_REFUND_RULES = get_config_loader().load("refund")

# Sprint 16：退款读取改走 RefundService Protocol（CLAUDE.md §9.3.2）。
# 越权防护 / 软删过滤 / 状态过滤 / 分页 cursor 等语义下沉到 MySQLRefundService，
# Tool 层只做 DTO→dict 适配。check_refundable 已在 Sprint 15 走 OrderService，不重复走 RefundService。


class RefundTool:
    """退款查询工具（静态方法集合；DB 访问经 RefundService Protocol）"""

    # 7 天无理由：已签收后超过 7 天不可退（来自 refund.yaml）
    REFUND_WINDOW_DAYS: int = _REFUND_RULES["REFUND_WINDOW_DAYS"]

    @staticmethod
    def list_user_refunds(user_id: int, limit: int = 20) -> list[dict]:
        """查某用户的所有退款记录"""
        svc = get_refund_service_factory().get_refund_service()
        refunds, _ = run_sync(svc.list_user_refunds(user_id, limit=limit))
        return [RefundTool._refund_dto_to_dict(r) for r in refunds]

    @staticmethod
    def get_refund_by_no(user_id: int, refund_no: str) -> Optional[dict]:
        """按退款号查（强制 user_id 防越权）"""
        svc = get_refund_service_factory().get_refund_service()
        refund = run_sync(svc.get_refund(user_id, refund_no))
        return RefundTool._refund_dto_to_dict(refund) if refund else None

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
        # Sprint 15：订单查询走 OrderService Protocol（含 user_id 越权防护 + 软删过滤）
        svc = get_order_service_factory().get_order_service()
        order = run_sync(svc.get_order(user_id, order_no))

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
    def _refund_dto_to_dict(r) -> dict:
        """RefundService.Refund (DTO) → dict（与旧 _to_dict 字段/格式一致）"""
        return {
            "refund_no": r.refund_no,
            "reason": r.reason,
            "status": r.status,
            "amount": float(r.amount),
            "create_time": r.create_time.isoformat() if r.create_time else None,
            "order_no": r.order_no,
        }