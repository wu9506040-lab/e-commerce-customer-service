"""
OrderModifyServiceFactory — 默认 MySQL 实现工厂（Sprint 18-C）

CLAUDE.md §9.3.2 支持模块替换：业务层（Tool）通过工厂拿 Protocol 实现，
接入方替换实现时只改工厂，不动业务代码。默认返回 MySQL 单例。

run_sync 复用：直接从 app.services.order.factory import（禁止复制 — spec §5）。
"""
from app.services.order.factory import run_sync
from app.services.order_modify.mysql_impl import MySQLOrderModifyService
from app.services.order_modify.protocols import OrderModifyService


class DefaultOrderModifyServiceFactory:
    """默认工厂（MySQL 实现，单例懒加载）"""

    def __init__(self) -> None:
        self._service: OrderModifyService | None = None

    def get_order_modify_service(self) -> OrderModifyService:
        if self._service is None:
            self._service = MySQLOrderModifyService()
        return self._service


# 进程内单例
_factory = DefaultOrderModifyServiceFactory()


def get_order_modify_service_factory() -> DefaultOrderModifyServiceFactory:
    """获取默认工厂单例（FastAPI Depends 注入点）"""
    return _factory


# 暴露 run_sync（外部 Tool 层无须再走一遍 order.factory）
__all__ = [
    "DefaultOrderModifyServiceFactory",
    "get_order_modify_service_factory",
    "run_sync",
]
