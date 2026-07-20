"""
PromotionRuleServiceFactory — 默认 YAML 实现工厂（Sprint 18 B）

CLAUDE.md §9.3.2 支持模块替换：业务层（未来 Tool）通过工厂拿 Protocol 实现，
接入方替换实现时只改工厂，不动业务代码。默认返回 YamlPromotionRuleService 单例。

**run_sync 复用**：本工厂不允许复制 run_sync 实现，统一从 order/factory 导入
（一致性 + 维护性，spec §1.4 关键约束）。
"""
from app.services.order.factory import run_sync  # noqa: F401  # 显式 re-export 供未来 Tool 层用
from app.services.promotion.protocols import PromotionRuleService
from app.services.promotion.yaml_impl import YamlPromotionRuleService


class DefaultPromotionRuleServiceFactory:
    """默认工厂（YAML 实现，单例懒加载）"""

    def __init__(self) -> None:
        self._service: PromotionRuleService | None = None

    def get_promotion_rule_service(self) -> PromotionRuleService:
        if self._service is None:
            self._service = YamlPromotionRuleService()
        return self._service


# 进程内单例（FastAPI Depends / Tool 层共享）
_factory = DefaultPromotionRuleServiceFactory()


def get_promotion_rule_service_factory() -> DefaultPromotionRuleServiceFactory:
    """获取默认工厂单例（FastAPI Depends 注入点）"""
    return _factory