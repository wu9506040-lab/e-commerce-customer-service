"""tests/context/test_resolver.py

M14 Stage 1：OrderContextResolver 0/1/N 决策单测

覆盖矩阵：
- ENABLE_ORDER_RESOLVER=False → DIRECT_ANSWER（短路）
- 非 order_query 意图 → DIRECT_ANSWER
- 匿名用户 + REQUIRE_LOGIN → ASK_LOGIN
- 用户提供有效 order_no → DIRECT_ANSWER
- 用户提供 order_no 但越权 → NOT_FOUND
- 用户提供无效格式 order_no → 走 0/1/N 路径
- ctx.current_order_no 命中 → DIRECT_ANSWER
- ctx.current_order_no 失效（订单被删）→ 清空走 0/1/N
- 0 订单 → ASK_LOGIN_OR_LIST
- 1 订单（自动选）→ DIRECT_ANSWER（用唯一那个）
- N 订单 + 1≤N≤5 → SHOW_PICKER（reason="multiple_orders_disambiguate"）
- N > MAX_PICKER_ITEMS 订单 → SHOW_PICKER（truncated=true）

边界：
- list_user_orders 抛异常 → DIRECT_ANSWER（fallback reason="resolver_error_fallback"）

所有测试 mock OrderTool（不连 DB）。
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# 让 `from app.services.context import ...` 能跑（项目在 backend/）
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# 测试环境变量（必须在 import settings 前设置）
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")

from app.core.config import settings  # noqa: E402
from app.services.context import OrderContextResolver, OrderResolverAction  # noqa: E402
from app.services.context.context_service import ConversationContext  # noqa: E402
from app.services.session_service import ANONYMOUS_USER_ID  # noqa: E402


# =============================================================
# 辅助
# =============================================================
def _make_order(order_no: str, status: str = "shipped") -> dict:
    return {
        "order_no": order_no,
        "status": status,
        "total_amount": 99.0,
        "create_time": "2026-07-16T10:00:00",
    }


def _make_ctx(**kwargs) -> ConversationContext:
    base = dict(
        session_id="test-session", user_id=1,
        last_intent=None, current_order_no=None, flow_state=None,
    )
    base.update(kwargs)
    return ConversationContext(**base)


# =============================================================
# 1. 灰度开关：关闭时短路
# =============================================================
class TestResolverDisabled:
    def test_resolver_disabled_returns_direct_answer(self):
        """ENABLE_ORDER_RESOLVER=False → DIRECT_ANSWER（不参与决策）"""
        with patch.object(settings, "ENABLE_ORDER_RESOLVER", False):
            r = OrderContextResolver()
            result = r.resolve(
                user_id=1, intent="order_query",
                entities={}, ctx=_make_ctx(),
            )
            assert result.action == OrderResolverAction.DIRECT_ANSWER
            assert result.reason == "resolver_disabled"


# =============================================================
# 2. 非 order_query 意图不参与决策
# =============================================================
class TestNonOrderIntent:
    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    def test_refund_query_skips(self):
        r = OrderContextResolver()
        for intent in ("refund_query", "product_query", "policy_query"):
            result = r.resolve(
                user_id=1, intent=intent, entities={}, ctx=_make_ctx(),
            )
            assert result.action == OrderResolverAction.DIRECT_ANSWER
            assert result.reason == "non_order_intent"


# =============================================================
# 3. 匿名用户
# =============================================================
class TestAnonymousUser:
    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    def test_anonymous_user_ask_login(self):
        """user_id=ANONYMOUS_USER_ID + REQUIRE_LOGIN=True → ASK_LOGIN"""
        r = OrderContextResolver()
        result = r.resolve(
            user_id=ANONYMOUS_USER_ID, intent="order_query",
            entities={}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.ASK_LOGIN
        assert result.reason == "anonymous_user"


# =============================================================
# 4. 用户提供有效 order_no
# =============================================================
class TestUserProvidedOrderNo:
    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_user_provided_order_no_hit(self, mock_tool):
        """entities.order_no 有效 + 归属正确 → DIRECT_ANSWER"""
        mock_tool.get_order_by_no.return_value = _make_order("ORD20260101ABC")
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={"order_no": "ORD20260101ABC"}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        assert result.reason == "user_provided_order_no"
        assert result.effective_order_no == "ORD20260101ABC"

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_user_provided_order_no_越权(self, mock_tool):
        """entities.order_no 格式正确但不属于当前用户 → NOT_FOUND"""
        mock_tool.get_order_by_no.return_value = None
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={"order_no": "ORD20260101ABC"}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.NOT_FOUND
        assert result.effective_order_no == "ORD20260101ABC"
        assert result.reason == "order_not_found_or_not_owned"

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    def test_user_provided_invalid_format_falls_through(self):
        """entities.order_no 格式无效 → 走 0/1/N 路径（不是 NOT_FOUND）"""
        r = OrderContextResolver()
        with patch(
            "app.services.context.order_context_resolver.OrderTool"
        ) as mock_tool:
            mock_tool.list_user_orders.return_value = [_make_order("ORD20260101AAA")]
            result = r.resolve(
                user_id=1, intent="order_query",
                entities={"order_no": "GARBAGE"}, ctx=_make_ctx(),
            )
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        assert result.reason == "only_one_order"


# =============================================================
# 5. ctx.current_order_no 命中
# =============================================================
class TestContextOrderNo:
    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_ctx_current_order_no_hit(self, mock_tool):
        """ctx.current_order_no 有效 → DIRECT_ANSWER（不走 list_user_orders）"""
        mock_tool.get_order_by_no.return_value = _make_order("ORD20260101XYZ")
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={}, ctx=_make_ctx(current_order_no="ORD20260101XYZ"),
        )
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        assert result.reason == "context_order_no_hit"
        assert result.effective_order_no == "ORD20260101XYZ"
        # 不应调 list_user_orders
        mock_tool.list_user_orders.assert_not_called()

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_ctx_current_order_no_stale(self, mock_tool):
        """ctx.current_order_no 失效（订单被删/越权）→ 走 0/1/N 路径"""
        mock_tool.get_order_by_no.return_value = None  # 失效
        mock_tool.list_user_orders.return_value = [_make_order("ORD20260101AAA")]
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={}, ctx=_make_ctx(current_order_no="ORD99999999XXX"),
        )
        # 0/1/N 路径：1 订单 → DIRECT_ANSWER
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        assert result.reason == "only_one_order"
        assert result.effective_order_no == "ORD20260101AAA"


# =============================================================
# 6. 0 / 1 / N 订单
# =============================================================
class TestOrderCount:
    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_zero_orders_ask_login_or_list(self, mock_tool):
        """0 订单 → ASK_LOGIN_OR_LIST"""
        mock_tool.list_user_orders.return_value = []
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.ASK_LOGIN_OR_LIST
        assert result.reason == "zero_orders"
        assert result.total_orders == 0

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_one_order_direct_answer(self, mock_tool):
        """1 订单 → DIRECT_ANSWER（自动用唯一那个）"""
        mock_tool.list_user_orders.return_value = [_make_order("ORD20260101AAA")]
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        assert result.reason == "only_one_order"
        assert result.effective_order_no == "ORD20260101AAA"
        assert result.total_orders == 1

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_n_orders_show_picker(self, mock_tool):
        """N=3 订单 → SHOW_PICKER（不带 truncated）"""
        mock_tool.list_user_orders.return_value = [
            _make_order("ORD20260101AAA"),
            _make_order("ORD20260201BBB"),
            _make_order("ORD20260301CCC"),
        ]
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.SHOW_PICKER
        assert result.reason == "multiple_orders_disambiguate"
        assert result.total_orders == 3
        assert result.truncated is False
        assert len(result.candidate_orders) == 3

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_many_orders_truncated(self, mock_tool):
        """N=10 订单（>MAX_PICKER_ITEMS=5）→ SHOW_PICKER（truncated=true，items 截断到 5）"""
        mock_tool.list_user_orders.return_value = [
            _make_order(f"ORD20260{i:03d}XXX") for i in range(1, 11)
        ]
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.SHOW_PICKER
        assert result.truncated is True
        assert result.total_orders == 10
        assert len(result.candidate_orders) == 5  # 截断到 MAX_PICKER_ITEMS


# =============================================================
# 7. 边界：list_user_orders 异常
# =============================================================
class TestResolverErrorFallback:
    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_list_user_orders_error_fallback(self, mock_tool):
        """list_user_orders 抛异常 → DIRECT_ANSWER（fallback，让 orchestrator 走老路径）"""
        mock_tool.get_order_by_no.return_value = None
        mock_tool.list_user_orders.side_effect = Exception("DB 连接失败")
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        assert result.reason == "resolver_error_fallback"


# =============================================================
# 8. order_no 格式校验
# =============================================================
class TestOrderNoValidation:
    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_order_no_lowercase_normalized(self, mock_tool):
        """entities.order_no 大小写不敏感（IGNORECASE）"""
        mock_tool.get_order_by_no.return_value = _make_order("ORD20260101ABC")
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={"order_no": "ord20260101abc"}, ctx=_make_ctx(),
        )
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        # 注意：result.effective_order_no 保留 entities 原值（小写），
        # OrderTool.get_order_by_no 内部会做大小写归一化
        assert result.effective_order_no == "ord20260101abc"

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    def test_order_no_empty_falls_through(self):
        """entities.order_no 为空字符串 → 视为无，走 0/1/N 路径"""
        with patch(
            "app.services.context.order_context_resolver.OrderTool"
        ) as mock_tool:
            mock_tool.list_user_orders.return_value = [_make_order("ORD20260101AAA")]
            r = OrderContextResolver()
            result = r.resolve(
                user_id=1, intent="order_query",
                entities={"order_no": ""}, ctx=_make_ctx(),
            )
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        assert result.reason == "only_one_order"

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    def test_order_no_none_falls_through(self):
        """entities.order_no 为 None → 走 0/1/N 路径"""
        with patch(
            "app.services.context.order_context_resolver.OrderTool"
        ) as mock_tool:
            mock_tool.list_user_orders.return_value = [_make_order("ORD20260101AAA")]
            r = OrderContextResolver()
            result = r.resolve(
                user_id=1, intent="order_query",
                entities={"order_no": None}, ctx=_make_ctx(),
            )
        assert result.action == OrderResolverAction.DIRECT_ANSWER
        assert result.reason == "only_one_order"


# =============================================================
# 9. OrderResolverResult.to_dict 序列化
# =============================================================
class TestResultSerialization:
    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    def test_to_dict_round_trip(self, mock_tool):
        """to_dict 返回 dict（便于 SSE JSON 序列化）"""
        mock_tool.list_user_orders.return_value = [
            _make_order("ORD20260101AAA"),
            _make_order("ORD20260201BBB"),
        ]
        r = OrderContextResolver()
        result = r.resolve(
            user_id=1, intent="order_query",
            entities={}, ctx=_make_ctx(),
        )
        d = result.to_dict()
        assert d["action"] == "show_picker"
        assert d["intent"] == "order_query"
        assert d["reason"] == "multiple_orders_disambiguate"
        assert d["total_orders"] == 2
        assert len(d["candidate_orders"]) == 2
        assert "effective_order_no" in d
        assert "truncated" in d