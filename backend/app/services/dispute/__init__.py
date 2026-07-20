"""services/dispute 模块 — DisputeService Protocol 抽象（Sprint 20 通用客服中台）"""
from app.services.dispute.protocols import (
    DisputeError,
    DisputeService,
    DisputeServiceFactory,
)
from app.services.dispute.yaml_impl import YamlDisputeService
from app.services.dispute.factory import (
    DefaultDisputeServiceFactory,
    get_dispute_service_factory,
)

__all__ = [
    "DisputeError",
    "DisputeService",
    "DisputeServiceFactory",
    "YamlDisputeService",
    "DefaultDisputeServiceFactory",
    "get_dispute_service_factory",
]
