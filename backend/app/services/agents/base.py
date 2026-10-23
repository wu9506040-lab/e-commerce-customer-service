"""专家 Agent 基座（V21 M3-1 · 多 Agent 协作制第一阶段）。

设计立场（对齐行业走向，ResolveAI / smartcommerce-agent 验证过的形态）：
- 意图域专家显式化为 **Agent 实体**（有名字、有职责边界、有独立可测的
  dispatch），而不是散在 orchestrator if/elif 里的分支——职责隔离、
  独立演化、失败隔离是多 Agent 的真实收益。
- **不做 Supervisor 全局编排**：分派仍由三级意图路由驱动（成本结构优势），
  专家层负责"职责内怎么做 + 何时转交"。这是协作制（collaboration），
  不是中心化调度（supervisor）。

转交（handoff）契约 v1：
- 专家可在 dispatch 前 refine(ctx) 修改上下文/重定向意图；
- dispatch 产出的事件流沿用现有 (event_type, data) 元组协议，
  供 chat.py / orchestrator 透传——**协议不变，编排者不变，只加实体层**。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentContext:
    """一次对话轮的专家执行上下文（orchestrator 组装，专家只读为主）。"""

    query: str
    user_id: int
    intent_result: dict
    session_id: Optional[str] = None
    order_no: Optional[str] = None
    sku: Optional[str] = None
    context_block: str = ""
    history: Optional[list[dict]] = None
    secondary_intent_block: str = ""
    search_queries: Optional[list[str]] = None
    channel: str = "web"
    buyer_unb: str = ""
    # 转交链路：refine 可写 redirect_intent，orchestrator 据此改派（最多一次，防环）
    redirect_intent: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


class AgentProtocol:
    """专家 Agent 统一接口（Protocol 语义：实现方无需继承，duck typing 即可）。

    子类/实现方必须提供：
      name: str                     —— 专家名（日志/trace/registry 键）
      intents: tuple[str, ...]      —— 负责的意图域
      dispatch(ctx) -> generator    —— 产出 (event_type, data) 事件流
    可选覆盖：
      refine(ctx)                   —— 分派前上下文精化 / 重定向
    """

    name: str = "base"
    intents: tuple[str, ...] = ()

    def refine(self, ctx: AgentContext) -> None:
        """分派前钩子：专家可在此做重定向（ctx.redirect_intent）或补充上下文。

        默认无操作。转交示例：OrderAgent 发现 query 含退款诉求 →
        ctx.redirect_intent = "refund_query"（orchestrator 最多执行一次改派）。
        """

    def dispatch(self, ctx: AgentContext) -> Generator[tuple[str, Any], None, None]:
        raise NotImplementedError(f"{self.name} 未实现 dispatch")

    def handles(self, intent: str) -> bool:
        return intent in self.intents
