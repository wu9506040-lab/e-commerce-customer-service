"""
RefundServiceFactory — 默认 MySQL 实现工厂（Sprint 16）

CLAUDE.md §9.3.2 支持模块替换：业务层（Tool）通过工厂拿 Protocol 实现，
接入方替换实现时只改工厂，不动业务代码。默认返回 MySQL 单例。

**run_sync 复用**：本工厂不允许复制 run_sync 实现，统一从 order/factory 导入
（一致性 + 维护性，spec §1.4 关键约束）。
"""
from app.services.order.factory import run_sync  # noqa: F401  # 显式 re-export 供 Tool 层用
from app.services.refund.mysql_impl import MySQLRefundService
from app.services.refund.protocols import RefundService


class DefaultRefundServiceFactory:
    """默认工厂（MySQL 实现，单例懒加载）"""

    def __init__(self) -> None:
        self._refund_service: RefundService | None = None

    def get_refund_service(self) -> RefundService:
        if self._refund_service is None:
            self._refund_service = MySQLRefundService()
        return self._refund_service


# 进程内单例（FastAPI Depends / Tool 层共享）
_factory = DefaultRefundServiceFactory()


def get_refund_service_factory() -> DefaultRefundServiceFactory:
    """获取默认工厂单例（FastAPI Depends 注入点）"""
    return _factory