"""services/after_sales 模块 — AfterSalesRuleService Protocol 抽象（Sprint 18 场景组 A）"""
from app.services.after_sales.protocols import (
    AfterSalesError,
    AfterSalesRuleService,
    AfterSalesRuleServiceFactory,
    OrderNotFoundForAdviceError,
)

__all__ = [
    "AfterSalesError",
    "AfterSalesRuleService",
    "AfterSalesRuleServiceFactory",
    "OrderNotFoundForAdviceError",
]