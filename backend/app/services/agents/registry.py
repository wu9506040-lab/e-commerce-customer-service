"""专家 Agent 注册表（V21 M3-1）：意图 → 专家实体。"""
from __future__ import annotations

from app.services.agents.base import AgentContext, AgentProtocol
from app.services.agents.experts import (
    OrderAgent,
    PolicyAgent,
    ProductAgent,
    RefundAgent,
)

_REGISTRY: dict[str, AgentProtocol] = {}
for _agent in (PolicyAgent(), ProductAgent(), OrderAgent(), RefundAgent()):
    for _intent in _agent.intents:
        _REGISTRY[_intent] = _agent


def get_expert(intent: str) -> Optional[AgentProtocol]:
    """按意图取专家；未注册意图返回 None（调用方自行兜底）。"""
    return _REGISTRY.get(intent)


def all_experts() -> dict[str, AgentProtocol]:
    return dict(_REGISTRY)


__all__ = [
    "AgentContext",
    "AgentProtocol",
    "get_expert",
    "all_experts",
]
