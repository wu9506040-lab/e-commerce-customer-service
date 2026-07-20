"""
OrderServiceFactory — 默认 MySQL 实现工厂（Sprint 15）

CLAUDE.md §9.3.2 支持模块替换：业务层（Tool）通过工厂拿 Protocol 实现，
接入方替换实现时只改工厂，不动业务代码。默认返回 MySQL 单例。
run_sync：把 Protocol 的 async 方法在 sync Tool 层安全执行的桥接。
"""
import asyncio
from typing import Any, Awaitable

from app.services.order.mysql_impl import MySQLOrderService, MySQLProductService
from app.services.order.protocols import OrderService, ProductService


def run_sync(coro: Awaitable[Any]) -> Any:
    """在 sync 上下文执行 async Protocol 方法（Tool 层全 sync；无 running loop 时 asyncio.run，兜底独立线程）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class DefaultOrderServiceFactory:
    """默认工厂（MySQL 实现，单例懒加载）"""

    def __init__(self) -> None:
        self._order_service: OrderService | None = None
        self._product_service: ProductService | None = None

    def get_order_service(self) -> OrderService:
        if self._order_service is None:
            self._order_service = MySQLOrderService()
        return self._order_service

    def get_product_service(self) -> ProductService:
        if self._product_service is None:
            self._product_service = MySQLProductService()
        return self._product_service


# 进程内单例（FastAPI Depends / Tool 层共享）
_factory = DefaultOrderServiceFactory()


def get_order_service_factory() -> DefaultOrderServiceFactory:
    """获取默认工厂单例（FastAPI Depends 注入点）"""
    return _factory
