"""services/supplement 模块 — SupplementRuleService Protocol 抽象（Sprint 20 通用客服中台）"""
from app.services.supplement.protocols import (
    SupplementError,
    SupplementRuleService,
    SupplementRuleServiceFactory,
)
from app.services.supplement.yaml_impl import YamlSupplementRuleService
from app.services.supplement.factory import (
    DefaultSupplementRuleServiceFactory,
    get_supplement_rule_service_factory,
)

__all__ = [
    "SupplementError",
    "SupplementRuleService",
    "SupplementRuleServiceFactory",
    "YamlSupplementRuleService",
    "DefaultSupplementRuleServiceFactory",
    "get_supplement_rule_service_factory",
]
