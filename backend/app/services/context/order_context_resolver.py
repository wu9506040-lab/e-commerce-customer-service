"""OrderContextResolver - 订单上下文决策（M14 核心）

按 CLAUDE.md §6 + §9.2.2：services/ 编排层，调 OrderTool + OrderLifecycle。
被 chat/orchestrator.py:_handle_order 调用，决定 0/1/N 情况下走什么路径。

设计：
- 灰度开关：ENABLE_ORDER_RESOLVER=False 时短路返 DIRECT_ANSWER（兼容老逻辑）
- 纯业务规则决策，不调 LLM（避免成本 + 延迟）
- 决策结果可被 Resolver 调度：前端根据 action 渲染不同卡片

决策矩阵（基于 order_query 意图）：
                ┌─ entities.order_no 有效 → DIRECT_ANSWER（详情已查）
                ├─ entities.order_no 失效 → NOT_FOUND（越权 / 不存在）
                │
  user_id=ANON → ASK_LOGIN（未登录走固定话术）
                │
  0 订单        ├→ ASK_LOGIN_OR_LIST（"您还没有订单" 兜底）
  1 订单        ├→ DIRECT_ANSWER（自动用那 1 个 order_no）
  N 订单 + ctx  ├→ SHOW_PICKER（reason="disambiguate"，让用户选）
  有 current_order_no

非 order_query 意图 → DIRECT_ANSWER（不参与决策）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.core.config import settings
from app.services.config_loader import get_config_loader
from app.services.context.context_service import ConversationContext
from app.services.session_service import ANONYMOUS_USER_ID
from app.tools.order_tool import OrderTool

logger = logging.getLogger(__name__)


# =============================================================
# Action 枚举
# =============================================================
class OrderResolverAction(str, Enum):
    """Resolver 决策结果（orchestrator 据此分派）"""

    # 让用户从 N 个订单中选（触发前端 OrderCard list 渲染）
    SHOW_PICKER = "show_picker"

    # 直接答（用户给了有效 order_no / 唯一 1 个订单时走这里）
    DIRECT_ANSWER = "direct_answer"

    # 订单不存在或不属于当前用户
    NOT_FOUND = "not_found"

    # 未登录（匿名用户查订单）
    ASK_LOGIN = "ask_login"

    # 已登录但 0 订单
    ASK_LOGIN_OR_LIST = "ask_login_or_list"


@dataclass
class OrderResolverResult:
    """Resolver 决策结果（带详情给前端 SSE meta.card 用）"""

    action: OrderResolverAction
    # 当前意图（透传，方便 orchestrator 拼 meta）
    intent: str = "order_query"
    # Resolver 决策原因（用于 audit + 前端 reason 字段）
    reason: str = ""
    # 用户已提供的 order_no（如有效）
    effective_order_no: Optional[str] = None
    # Resolver 推断出的候选订单（SHOW_PICKER 时填）
    candidate_orders: list = field(default_factory=list)
    # 总订单数（0 / 1 / N），用于指标统计
    total_orders: int = 0
    # 候选是否被截断（N>MAX_PICKER_ITEMS 时 True）
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "intent": self.intent,
            "reason": self.reason,
            "effective_order_no": self.effective_order_no,
            "candidate_orders": self.candidate_orders,
            "total_orders": self.total_orders,
            "truncated": self.truncated,
        }


# =============================================================
# 业务规则（启动期加载一次，来自 config/business_rules/order_context.yaml）
# =============================================================
_RULES = get_config_loader().load("order_context")

MAX_PICKER_ITEMS: int = int(_RULES.get("MAX_PICKER_ITEMS", 5))
REQUIRE_LOGIN: bool = bool(_RULES.get("REQUIRE_LOGIN", True))
# 订单号正则（与 intent.yaml 同步；这里只用于 Resolver 校验 entities.order_no 合法性）
import re as _re
ORDER_NO_PATTERN: _re.Pattern = _re.compile(
    _RULES.get("ORDER_NO_PATTERN", r"^ORD\d{8}[A-Z0-9]{3,6}$"),
    _re.IGNORECASE,
)


# =============================================================
# Resolver
# =============================================================
class OrderContextResolver:
    """订单上下文决策器（0/1/N + 灰度开关）"""

    def __init__(self) -> None:
        pass

    def resolve(
        self,
        user_id: int,
        intent: str,
        entities: dict,
        ctx: ConversationContext,
    ) -> OrderResolverResult:
        """决策主入口。

        Args:
            user_id: 用户 ID
            intent: 当前意图
            entities: 实体抽取结果（order_no / sku）
            ctx: 当前会话上下文

        Returns:
            OrderResolverResult
        """
        # 灰度开关：关闭时返 DIRECT_ANSWER（不参与决策，orchestrator 走老逻辑）
        if not settings.ENABLE_ORDER_RESOLVER:
            return OrderResolverResult(
                action=OrderResolverAction.DIRECT_ANSWER,
                intent=intent,
                reason="resolver_disabled",
            )

        # 支持 order_query + refund_query（0/1/N 决策 intent-agnostic，与意图无关）
        # product_query / policy_query 仍走 DIRECT_ANSWER（不需要订单解析）
        if intent not in {"order_query", "refund_query"}:
            return OrderResolverResult(
                action=OrderResolverAction.DIRECT_ANSWER,
                intent=intent,
                reason="non_order_intent",
            )

        # 1. 匿名用户
        if user_id == ANONYMOUS_USER_ID:
            if REQUIRE_LOGIN:
                return OrderResolverResult(
                    action=OrderResolverAction.ASK_LOGIN,
                    intent=intent,
                    reason="anonymous_user",
                )
            # REQUIRE_LOGIN=False 时降级（保留可配置性，但默认 True）

        # 2. 用户已提供有效 order_no → DIRECT_ANSWER
        provided_order_no = (entities or {}).get("order_no")
        if provided_order_no and self._is_valid_order_no(provided_order_no):
            # 校验归属（防越权）
            order = OrderTool.get_order_by_no(user_id, provided_order_no)
            if order is None:
                return OrderResolverResult(
                    action=OrderResolverAction.NOT_FOUND,
                    intent=intent,
                    reason="order_not_found_or_not_owned",
                    effective_order_no=provided_order_no,
                )
            return OrderResolverResult(
                action=OrderResolverAction.DIRECT_ANSWER,
                intent=intent,
                reason="user_provided_order_no",
                effective_order_no=provided_order_no,
            )

        # 3. ctx 已携带 current_order_no → DIRECT_ANSWER（用户从 OrderCard 跳入）
        if ctx.current_order_no:
            order = OrderTool.get_order_by_no(user_id, ctx.current_order_no)
            if order is None:
                # ctx.current_order_no 已失效（订单被删/不属于当前用户）→ 清空，走下一步
                logger.info(
                    f"order_resolver: ctx.current_order_no 失效, "
                    f"order_no={ctx.current_order_no}, user_id={user_id}"
                )
            else:
                return OrderResolverResult(
                    action=OrderResolverAction.DIRECT_ANSWER,
                    intent=intent,
                    reason="context_order_no_hit",
                    effective_order_no=ctx.current_order_no,
                )

        # 4. 列最近订单（0/1/N）
        try:
            recent_orders = OrderTool.list_user_orders(
                user_id, limit=MAX_PICKER_ITEMS + 1,  # 多取 1 用来判断是否截断
            )
        except Exception as e:
            # DB 故障 → 降级 DIRECT_ANSWER（让 orchestrator 走老路径兜底）
            logger.warning(
                f"order_resolver: list_user_orders failed: user_id={user_id}, {e}"
            )
            return OrderResolverResult(
                action=OrderResolverAction.DIRECT_ANSWER,
                intent=intent,
                reason="resolver_error_fallback",
            )

        total = len(recent_orders)
        truncated = total > MAX_PICKER_ITEMS
        displayed = recent_orders[:MAX_PICKER_ITEMS]

        if total == 0:
            return OrderResolverResult(
                action=OrderResolverAction.ASK_LOGIN_OR_LIST,
                intent=intent,
                reason="zero_orders",
                total_orders=0,
            )

        if total == 1:
            # 唯一 1 个订单 → 自动用（避免让用户从 1 个订单里"选"）
            only_order = recent_orders[0]
            return OrderResolverResult(
                action=OrderResolverAction.DIRECT_ANSWER,
                intent=intent,
                reason="only_one_order",
                effective_order_no=only_order.get("order_no"),
                candidate_orders=displayed,
                total_orders=total,
            )

        # N >= 2 → SHOW_PICKER（让用户选）
        return OrderResolverResult(
            action=OrderResolverAction.SHOW_PICKER,
            intent=intent,
            reason="multiple_orders_disambiguate",
            candidate_orders=displayed,
            total_orders=total,
            truncated=truncated,
        )

    # ---------- 私有 ----------

    @staticmethod
    def _is_valid_order_no(order_no: str) -> bool:
        """校验 order_no 格式合法性（与 intent.yaml ORDER_NO_RE_PATTERN 同步）。"""
        if not order_no or not isinstance(order_no, str):
            return False
        return bool(ORDER_NO_PATTERN.match(order_no))


# =============================================================
# 工厂入口
# =============================================================
_resolver: Optional[OrderContextResolver] = None


def get_order_context_resolver() -> OrderContextResolver:
    """工厂入口。业务模块**只能**通过此函数获取（禁止直接 new）。"""
    global _resolver
    if _resolver is None:
        _resolver = OrderContextResolver()
    return _resolver


def reset_order_context_resolver() -> None:
    """测试钩子：重置单例（仅供 test fixtures）。"""
    global _resolver
    _resolver = None