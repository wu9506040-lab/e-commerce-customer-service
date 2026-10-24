"""专家 Agent 注册表（V21 M3-1 专科 4 个 + M3-3 全科兜底 = 五专家）。"""
from __future__ import annotations

from typing import Optional

from app.services.agents.base import AgentContext, AgentProtocol
from app.services.agents.experts import (
    OrderAgent,
    PolicyAgent,
    ProductAgent,
    RefundAgent,
)
from app.services.agents.generalist import GeneralistAgent

_REGISTRY: dict[str, AgentProtocol] = {}
for _agent in (
    PolicyAgent(), ProductAgent(), OrderAgent(), RefundAgent(),
    GeneralistAgent(),  # V21 M3-3：第五专家——开放问题 FC 兜底（intents=("general",)）
):
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
    "GeneralistAgent",
]
