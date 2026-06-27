"""
退款服务 - 编排 RefundTool + PolicyService（复合路径）

按 PROJECT_DESIGN.md §3 + §7：
- refund_query 走「Tool（结构化状态）+ Policy RAG（条款）」双源
- ResponseSynthesizer（M4）会用本服务的输出做最终融合
"""
import logging
from typing import Optional

from app.services.policy_service import PolicyService
from app.tools.refund_tool import RefundTool

logger = logging.getLogger(__name__)


class RefundService:
    """退款业务服务（编排层：tool + policy 融合）"""

    @staticmethod
    def list_user_refunds(user_id: int) -> list[dict]:
        """查用户的所有退款记录"""
        return RefundTool.list_user_refunds(user_id)

    @staticmethod
    def get_refund_detail(user_id: int, refund_no: str) -> Optional[dict]:
        """查退款详情"""
        return RefundTool.get_refund_by_no(user_id, refund_no)

    @staticmethod
    def check_refundable_with_policy(user_id: int, order_no: str, query: str = "") -> dict:
        """
        退款可行性检查 + 政策条款召回（复合路径）

        Args:
            user_id: 用户 ID
            order_no: 订单号
            query: 用户原始问题（用于 policy RAG）

        Returns:
            {
                "tool_result": {...},     # RefundTool.check_refundable 输出
                "policy_docs": [...],     # PolicyService.search_policy 输出
                "synthesizable": bool,    # 至少一边有结果
            }
        """
        # 1. tool：状态 + 简单规则
        tool_result = RefundTool.check_refundable(user_id, order_no)

        # 2. policy RAG：召回相关条款
        policy_query = query if query else f"退款规则 {tool_result.get('order_status', '')}"
        policy_docs = PolicyService.search_policy(policy_query, top_k=3)

        return {
            "tool_result": tool_result,
            "policy_docs": policy_docs,
            "synthesizable": bool(tool_result.get("refundable") is not None) or len(policy_docs) > 0,
        }