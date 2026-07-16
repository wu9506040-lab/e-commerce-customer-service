"""BusinessFlowFactory（M14 §10 阶段 3）

意图 → Flow 实例的路由入口。
按 plan §10：现阶段只支持 refund_query → RefundFlow（YAGNI 兜底）。
"""
from __future__ import annotations

import logging
from typing import Any, Generator, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)


class BusinessFlowFactory:
    """BusinessFlow 工厂

    单一入口：create(intent, ...) → Optional[Flow]
    返回 None 表示该 intent 暂不参与 Flow 抽象（orchestrator 走老路径）

    灰度开关：settings.ENABLE_BUSINESS_FLOW=False 时一律返回 None
    """

    @staticmethod
    def create(
        intent: str,
        query: str,
        user_id: int,
        intent_result: dict,
        order_no: Optional[str] = None,
        context_block: str = "",
        history: Optional[list[dict]] = None,
    ):
        """工厂入口：按 intent 返回对应 Flow 实例

        Returns:
            Flow 实例 或 None
            None 表示：未启用 / 该 intent 无对应 Flow
        """
        # 灰度开关：默认关闭 → 不参与调度
        if not settings.ENABLE_BUSINESS_FLOW:
            return None

        # 延迟导入：避免循环依赖
        from app.services.business_flow.refund_flow import RefundFlow

        if intent == "refund_query":
            logger.info(
                f"BusinessFlowFactory → RefundFlow: intent=refund_query user_id={user_id}"
            )
            return RefundFlow(
                query=query,
                user_id=user_id,
                intent_result=intent_result,
                order_no=order_no,
                context_block=context_block,
                history=history,
            )

        # 其他 intent（order_query / product_query / policy_query）暂不抽象
        # — YAGNI：等出现第 2 个 Flow 时再扩
        return None