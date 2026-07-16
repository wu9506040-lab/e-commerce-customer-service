"""M14 Context Protocol（就近放置 · CLAUDE.md §7.3）

业务模块依赖这些 Protocol，不直接 import 具体类。
便于 mock 和未来替换实现（如 ContextService 换 Redis-only）。
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from app.services.context.context_service import ConversationContext
from app.services.context.order_context_resolver import OrderResolverResult


@runtime_checkable
class ContextServiceProtocol(Protocol):
    """会话上下文服务：session 级 KV 状态读写。

    所有方法对异常采取 best-effort 策略（DB 故障不阻塞主流程）。
    """

    def load(self, session_id: str, user_id: int) -> ConversationContext:
        """加载会话上下文（不存在则返回默认值）。

        Args:
            session_id: 会话 UUID
            user_id: 用户 ID（用于新建行的归属）

        Returns:
            ConversationContext 实例；DB 异常返回空上下文（best-effort）。
        """
        ...

    def update(
        self,
        session_id: str,
        user_id: int,
        last_intent: Optional[str] = None,
        current_order_no: Optional[str] = None,
        flow_state: Optional[str] = None,
    ) -> bool:
        """部分更新会话上下文（None 字段不覆盖现有值）。

        Returns:
            True=成功；False=失败（仅 warning 不抛）。
        """
        ...


@runtime_checkable
class OrderContextResolverProtocol(Protocol):
    """订单上下文 Resolver：决定 0/1/N 情况下走什么路径。

    输入：
    - user_id + intent + entities + ctx
    输出：
    - OrderResolverResult {action, ...}

    不直接调 LLM，纯业务规则决策。
    """

    def resolve(
        self,
        user_id: int,
        intent: str,
        entities: dict,
        ctx: ConversationContext,
    ) -> OrderResolverResult:
        """根据用户上下文决定下一步动作。

        Args:
            user_id: 用户 ID
            intent: 当前意图（order_query / refund_query 等）
            entities: 实体抽取结果（order_no / sku 等）
            ctx: 当前会话上下文

        Returns:
            OrderResolverResult（action + 详情）
        """
        ...