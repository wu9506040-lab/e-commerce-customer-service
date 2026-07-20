"""
InvoiceServiceFactory — 默认 YAML 实现工厂（Sprint 20 通用客服中台）

CLAUDE.md §9.3.2 支持模块替换：业务层（Tool / Agent）通过工厂拿 Protocol 实现，
接入方替换实现时只改工厂，不动业务代码。默认返回 YAML 单例。
"""
from app.services.invoice.protocols import InvoiceService
from app.services.invoice.yaml_impl import YamlInvoiceService


class DefaultInvoiceServiceFactory:
    """默认工厂（YAML 实现，单例懒加载）"""

    def __init__(self) -> None:
        self._svc: InvoiceService | None = None

    def get_invoice_service(self) -> InvoiceService:
        if self._svc is None:
            self._svc = YamlInvoiceService()
        return self._svc


# 进程内单例（FastAPI Depends / Tool 层共享）
_factory = DefaultInvoiceServiceFactory()


def get_invoice_service_factory() -> DefaultInvoiceServiceFactory:
    """获取默认工厂单例（FastAPI Depends 注入点）"""
    return _factory
