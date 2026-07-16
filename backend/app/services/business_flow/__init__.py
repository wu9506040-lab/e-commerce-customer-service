"""
BusinessFlow 抽象（M14 §10 阶段 3）

设计取舍（plan §10 阶段 3 风险）：
- 只写 1 个具体 Flow（RefundFlow）；不抽 Base 抽象类（CLAUDE.md §3.3 YAGNI）
- 用 typing.Protocol 做结构类型（不强制继承）
- Factory 返回 Optional[Flow]，调用方按需 dispatch
- 灰度开关 ENABLE_BUSINESS_FLOW=False 默认关闭（向后兼容）

单一真相源：orchestrator.refund_query / order_query 分派入口
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generator, Optional, Tuple

from app.services.business_flow.protocols import Flow
from app.services.business_flow.refund_flow import RefundFlow

if TYPE_CHECKING:
    from app.services.business_flow.factory import BusinessFlowFactory


__all__ = [
    "Flow",
    "RefundFlow",
    "BusinessFlowFactory",
    "create_business_flow",
]


def create_business_flow(
    intent: str,
    query: str,
    user_id: int,
    intent_result: dict,
    order_no: Optional[str] = None,
    context_block: str = "",
    history: Optional[list[dict]] = None,
) -> Optional[Flow]:
    """工厂入口：按 intent 返回 Flow 实例；不参与时返回 None

    Args:
        intent: IntentService.classify 返回的 intent（order_query/refund_query/...）
        其他参数：透传给具体 Flow 构造函数

    Returns:
        Flow 实例 或 None（说明该 intent 暂不参与 Flow 抽象）
    """
    # 延迟导入：避免循环依赖
    from app.services.business_flow.factory import BusinessFlowFactory
    return BusinessFlowFactory.create(
        intent=intent,
        query=query,
        user_id=user_id,
        intent_result=intent_result,
        order_no=order_no,
        context_block=context_block,
        history=history,
    )