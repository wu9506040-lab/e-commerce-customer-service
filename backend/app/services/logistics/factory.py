"""
LogisticsServiceFactory — 默认 Mock 实现工厂（Sprint 16）

CLAUDE.md §9.3.2 支持模块替换：业务层（Tool）通过工厂拿 Protocol 实现，
接入方替换实现时只改工厂，不动业务代码。默认返回 Mock 单例。

**run_sync 复用**：本工厂不允许复制 run_sync 实现，统一从 order/factory 导入
（一致性 + 维护性，spec §1.4 / §2.4 关键约束）。
"""
from app.services.logistics.mock_impl import MockLogisticsService
from app.services.logistics.protocols import LogisticsService
from app.services.order.factory import run_sync  # noqa: F401  # 显式 re-export 供 Tool 层用


class DefaultLogisticsServiceFactory:
    """默认工厂（Mock 实现，单例懒加载）"""

    def __init__(self) -> None:
        self._logistics_service: LogisticsService | None = None

    def get_logistics_service(self) -> LogisticsService:
        if self._logistics_service is None:
            self._logistics_service = MockLogisticsService()
        return self._logistics_service


# 进程内单例（FastAPI Depends / Tool 层共享）
_factory = DefaultLogisticsServiceFactory()


def get_logistics_service_factory() -> DefaultLogisticsServiceFactory:
    """获取默认工厂单例（FastAPI Depends 注入点）"""
    return _factory