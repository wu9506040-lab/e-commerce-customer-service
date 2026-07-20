"""
AfterSalesTool - 售后规则咨询 Tool（Sprint 19）

按 CLAUDE.md §9.3.3：所有 AI 能力通过 Provider/Service 抽象，业务模块不直接调 SDK。
按 CLAUDE.md §9.7 自检 #3：Protocol 先于实现（依赖 AfterSalesRuleService Protocol，
                   不直连 YamlAfterSalesRuleService 具体类）。

接口就近原则（§7.3）：本 Tool 类放在 app/tools/ 根目录（与 OrderTool / ProductTool 同级）。

V2 范围（spec §2.1）：
- 3 个静态方法：get_refund_reason_advice / get_shipping_insurance_info / get_refund_type_advice
- 只读操作，无 confirmed 参数
- 走工厂拿 Protocol 实现 → run_sync 调 async 方法 → DTO.model_dump() → dict
- 任何异常路径返 {"error": "..."} dict（让 LLM 重试或换工具，不阻断主循环）
"""
from __future__ import annotations

import logging
from typing import Optional

from app.schemas.business import (
    RefundReasonAdvice,
    RefundTypeAdvice,
    ShippingInsuranceInfo,
)
from app.services.after_sales.factory import get_after_sales_rule_service_factory
from app.services.after_sales.protocols import AfterSalesRuleService
from app.services.order.factory import run_sync

logger = logging.getLogger(__name__)


def _to_dict_or_error(dto, err_tag: str) -> dict:
    """Pydantic v2 DTO → dict；None fallback 返 error dict。

    设计：保持调用方（dispatch / Agent runner）只消费 dict 的契约；
    Service 返 None 表示「无适用规则」（如订单不属于该 user），Tool 层用 error dict 表达。
    """
    if dto is None:
        return {"error": f"{err_tag}: no advice available"}
    return dto.model_dump()


class AfterSalesTool:
    """售后规则咨询 Tool（只读；V2 仅 Service 接口适配，无业务逻辑）

    方法均静态；Tool 类仅做 DTO→dict 适配 + 工厂拿 Service + run_sync 桥接，
    不实现任何售后规则（规则在 AfterSalesRuleService / after_sales.yaml）。
    """

    # =============================================================
    # 1. 退款原因填写指导
    # =============================================================
    @staticmethod
    def get_refund_reason_advice(
        user_id: int, order_no: str, reason_category: str,
    ) -> dict:
        """退款原因填写指导：建议具体原因文字 + 需要的凭证 + 成功率提示。

        Args:
            user_id: 用户 ID（防越权；AfterSalesRuleService 内部校验）
            order_no: 订单号
            reason_category: 退款原因类别（quality / no_reason / size /
                            not_as_described / late / other；未知值 fallback 到 other）

        Returns:
            RefundReasonAdvice.model_dump() dict
            或 {"error": "..."} dict（异常路径）
        """
        try:
            svc: AfterSalesRuleService = (
                get_after_sales_rule_service_factory().get_after_sales_rule_service()
            )
            advice: Optional[RefundReasonAdvice] = run_sync(
                svc.get_refund_reason_advice(user_id, order_no, reason_category),
            )
            return _to_dict_or_error(advice, "refund_reason_advice")
        except Exception as e:
            logger.warning(
                f"AfterSalesTool.get_refund_reason_advice 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}

    # =============================================================
    # 2. 运费险规则
    # =============================================================
    @staticmethod
    def get_shipping_insurance_info(order_no: str, return_status: str) -> dict:
        """运费险规则：哪些情况赔 / 赔多少 / 多久到账。

        Args:
            order_no: 订单号
            return_status: 退货状态（return_shipped / return_received / refunded）

        Returns:
            ShippingInsuranceInfo.model_dump() dict
            或 {"error": "..."} dict
        """
        try:
            svc: AfterSalesRuleService = (
                get_after_sales_rule_service_factory().get_after_sales_rule_service()
            )
            info: Optional[ShippingInsuranceInfo] = run_sync(
                svc.get_shipping_insurance_info(order_no, return_status),
            )
            return _to_dict_or_error(info, "shipping_insurance_info")
        except Exception as e:
            logger.warning(
                f"AfterSalesTool.get_shipping_insurance_info 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}

    # =============================================================
    # 3. 仅退款 vs 退货退款建议
    # =============================================================
    @staticmethod
    def get_refund_type_advice(user_id: int, order_no: str) -> dict:
        """仅退款 vs 退货退款建议。

        Args:
            user_id: 用户 ID（防越权）
            order_no: 订单号

        Returns:
            RefundTypeAdvice.model_dump() dict
            或 {"error": "..."} dict
        """
        try:
            svc: AfterSalesRuleService = (
                get_after_sales_rule_service_factory().get_after_sales_rule_service()
            )
            advice: Optional[RefundTypeAdvice] = run_sync(
                svc.get_refund_type_advice(user_id, order_no),
            )
            return _to_dict_or_error(advice, "refund_type_advice")
        except Exception as e:
            logger.warning(
                f"AfterSalesTool.get_refund_type_advice 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}


__all__ = ["AfterSalesTool"]