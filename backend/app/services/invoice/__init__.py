"""services/invoice 模块 — InvoiceService Protocol 抽象（Sprint 20 通用客服中台）"""
from app.services.invoice.protocols import (
    InvoiceService,
    InvoiceServiceFactory,
)
from app.schemas.business import InvoiceError, InvoiceNotEligibleError
from app.services.invoice.yaml_impl import YamlInvoiceService
from app.services.invoice.factory import (
    DefaultInvoiceServiceFactory,
    get_invoice_service_factory,
)

__all__ = [
    "InvoiceError",
    "InvoiceNotEligibleError",
    "InvoiceService",
    "InvoiceServiceFactory",
    "YamlInvoiceService",
    "DefaultInvoiceServiceFactory",
    "get_invoice_service_factory",
]
