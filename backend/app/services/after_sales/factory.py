"""
AfterSalesRuleServiceFactory — 默认 YAML 实现工厂（Sprint 18 场景组 A）

CLAUDE.md §9.3.2 支持模块替换：业务层（Tool / Agent）通过工厂拿 Protocol 实现，
接入方替换实现时只改工厂，不动业务代码。默认返回 YAML 单例。

V2 范围：纯 Service 接口暴露，**不接入 Tool 层**（spec §5 禁止事项）。
"""
from app.services.after_sales.protocols import AfterSalesRuleService
from app.services.after_sales.yaml_impl import YamlAfterSalesRuleService


class DefaultAfterSalesRuleServiceFactory:
    """默认工厂（YAML 实现，单例懒加载）"""

    def __init__(self) -> None:
        self._svc: AfterSalesRuleService | None = None

    def get_after_sales_rule_service(self) -> AfterSalesRuleService:
        if self._svc is None:
            self._svc = YamlAfterSalesRuleService()
        return self._svc


# 进程内单例（FastAPI Depends / Tool 层共享）
_factory = DefaultAfterSalesRuleServiceFactory()


def get_after_sales_rule_service_factory() -> DefaultAfterSalesRuleServiceFactory:
    """获取默认工厂单例（FastAPI Depends 注入点）"""
    return _factory