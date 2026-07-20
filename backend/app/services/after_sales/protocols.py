"""
AfterSalesRuleService Protocol（CLAUDE.md §9.3.2 支持模块替换）

V2 范围：纯规则查询，不调 LLM，不写订单。
业务规则从 config/business_rules/after_sales.yaml 加载（§9.4.2）。
默认实现：YamlAfterSalesRuleService（静态规则 + OrderService 查询，越权防护）。

设计（CLAUDE.md §9.7 自检 5 问）：
- Q1 业务模块依赖此 Protocol，不依赖具体实现
- Q3 Protocol 先于实现（本文件先定义签名）
- 方法全 async（spec §8 决策 #4；FastAPI 友好）

接入方：自建业务中台 / 客服系统实现此接口即可对接 AI 客服售后规则咨询。
"""
from typing import List, Optional, Protocol, runtime_checkable

from app.schemas.business import (
    RefundReasonAdvice,
    RefundTypeAdvice,
    ShippingInsuranceInfo,
)


@runtime_checkable
class AfterSalesRuleService(Protocol):
    """售后规则咨询协议"""

    async def get_refund_reason_advice(
        self, user_id: int, order_no: str, reason_category: str,
    ) -> RefundReasonAdvice:
        """退款原因填写指导：告诉用户选哪个原因更容易通过 + 需要提供什么凭证

        reason_category 取值：quality / no_reason / not_as_described / size / late / other
        不存在的 category → fallback 到 other 类别（spec §2.5 #8 容错）。
        """
        ...

    async def get_shipping_insurance_info(
        self, order_no: str, return_status: str,
    ) -> ShippingInsuranceInfo:
        """运费险规则：哪些情况赔 / 赔多少 / 多久到账

        return_status 取值：return_shipped / return_received / refunded
        仅 return_received / refunded 在 YAML eligible_statuses 中（spec §2.4）。
        """
        ...

    async def get_refund_type_advice(
        self, user_id: int, order_no: str,
    ) -> RefundTypeAdvice:
        """仅退款 vs 退货退款：该订单适合哪种？

        决策依据：订单 status + 金额 + reason_category 推测（V2 简化）。
        """
        ...


@runtime_checkable
class AfterSalesRuleServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_after_sales_rule_service(self) -> AfterSalesRuleService: ...


# === 异常类（CLAUDE.md §9.3.1 五件套之「异常处理」） ===
class AfterSalesError(Exception):
    """售后规则服务基类异常"""


class OrderNotFoundForAdviceError(AfterSalesError):
    """订单不存在或越权时抛出（get_refund_type_advice / get_refund_reason_advice 用）"""