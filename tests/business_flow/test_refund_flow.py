"""tests/business_flow/test_refund_flow.py

M14 Stage 3 + 2026-07-18 改造：RefundFlow 单测

覆盖：
1. ENABLE_BUSINESS_FLOW=False → factory.create() 返 None（短路）
2. ENABLE_BUSINESS_FLOW=True + refund_query → RefundFlow 实例
3. ENABLE_BUSINESS_FLOW=True + 非 refund_query → None（YAGNI：仅 1 个 Flow）
4. RefundFlow.run()：匿名用户短路（meta.flow_stage=fetch_order + NO_LOGIN_PROMPT）
5. RefundFlow.run()：无 order_no + 0 订单 → ASK_LOGIN_OR_LIST（2026-07-18 改造后）
6. RefundFlow.run()：无 order_no + 1 订单 → DIRECT_ANSWER 自动用 + 走 LangGraph
7. RefundFlow.run()：无 order_no + N 订单 → SHOW_PICKER + meta.card
8. RefundFlow.run()：有 order_no 走 LangGraph → yield meta.flow_stage + token
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# 让 `from app.services...` 能跑
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# 测试环境变量（必须在 import settings 前设置）
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")

from app.core.config import settings  # noqa: E402
from app.services.business_flow.factory import BusinessFlowFactory  # noqa: E402
from app.services.business_flow.refund_flow import RefundFlow  # noqa: E402
from app.services.session_service import ANONYMOUS_USER_ID  # noqa: E402


# =============================================================
# 1. 灰度开关
# =============================================================
class TestFlowDisabled:
    def test_disabled_returns_none(self):
        """ENABLE_BUSINESS_FLOW=False → factory.create() 返 None（灰度短路）"""
        with patch.object(settings, "ENABLE_BUSINESS_FLOW", False):
            flow = BusinessFlowFactory.create(
                intent="refund_query",
                query="我要退款",
                user_id=1,
                intent_result={"intent": "refund_query", "entities": {}},
            )
            assert flow is None


# =============================================================
# 2. Factory 路由
# =============================================================
class TestFactoryRouting:
    @patch.object(settings, "ENABLE_BUSINESS_FLOW", True)
    def test_refund_query_returns_refund_flow(self):
        """refund_query → RefundFlow 实例"""
        flow = BusinessFlowFactory.create(
            intent="refund_query",
            query="我要退款",
            user_id=1,
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
        )
        assert isinstance(flow, RefundFlow)
        assert flow.name == "refund"

    @patch.object(settings, "ENABLE_BUSINESS_FLOW", True)
    def test_non_refund_intent_returns_none(self):
        """非 refund_query → None（YAGNI：现阶段只抽象 refund）"""
        for intent in ("order_query", "product_query", "policy_query", "greeting"):
            flow = BusinessFlowFactory.create(
                intent=intent,
                query="测试",
                user_id=1,
                intent_result={"intent": intent, "entities": {}},
            )
            assert flow is None, f"{intent} should not have Flow"


# =============================================================
# 3. RefundFlow 短路路径
# =============================================================
class TestRefundFlowShortcuts:
    """不依赖 LangGraph 的短路分支"""

    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_anonymous_user_short_circuits(self, _mock_extract):
        """user_id=ANONYMOUS_USER_ID → NO_LOGIN_PROMPT"""
        flow = RefundFlow(
            query="我要退款",
            user_id=ANONYMOUS_USER_ID,
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
        )
        events = list(flow.run())
        # 第一个事件必须是 meta（fetch_order stage）
        assert events[0][0] == "meta"
        assert events[0][1]["flow_stage"] == "fetch_order"
        assert events[0][1]["v3_engine"] == "langgraph"
        # 后续是 token 事件（NO_LOGIN_PROMPT 切片）
        token_events = [e for e in events if e[0] == "token"]
        assert len(token_events) > 0, "匿名用户应至少 yield 一个 token（NO_LOGIN_PROMPT 切片）"

    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_no_order_no_short_circuits_legacy_check_replaced(self, _mock_extract):
        """2026-07-18 改造：原"无 order_no → 请提供订单号"已被 Resolver 自动解析取代。

        旧逻辑（防串单 M9.5）：直接要用户提供订单号 → 删
        新逻辑（真实业务场景）：走 Resolver 0/1/N 决策
          - 0 单 → ASK_LOGIN_OR_LIST（"您当前没有订单"）
          - 1 单 → DIRECT_ANSWER + 走 LangGraph
          - N 单 → SHOW_PICKER + meta.card

        本测试已被 test_no_order_no_zero_orders / test_no_order_no_one_order /
        test_no_order_no_n_orders 取代。保留为空壳以提醒迁移完成。
        """
        pass  # 原 test_no_order_no_short_circuits 已被拆分到 TestRefundFlowAutoResolve


# =============================================================
# 4. RefundFlow LangGraph 路径（mock stream）
# =============================================================
class TestRefundFlowLangGraph:
    """mock LangGraph stream() → 验证 flow_stage 推送 + token 输出"""

    @patch("app.services.business_flow.refund_flow.OrderTool")
    @patch("app.services.business_flow.refund_flow.refund_graph_app")
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_judge_yields_meta_with_flow_stage(self, _mock_extract, mock_app, mock_tool):
        """LangGraph decide node → yield meta.flow_stage='decide' + refundable + reason

        M14 V3 修复：
        - judge 节点改名 decide（refund_graph.py:733）
        - RefundFlow.run() 新增 OrderTool.get_order_by_no 调用（refund_flow.py:330），需 mock
        """
        # V3 新增：OrderTool.get_order_by_no mock（line 330 真实调用，否则本地无 DB 必 fail）
        mock_tool.get_order_by_no.return_value = {
            "order_no": "ORD001",
            "status": "delivered",
            "create_time": "2026-07-15T10:00:00",
            "total_amount": 299.0,
        }
        mock_app.stream.return_value = [
            {"fetch_order": {"order_info": {"order_no": "ORD001", "status": "delivered"}, "days_since_order": 3}},
            {"decide": {"refundable": True, "reason": "已签收 3 天，在 7 天内", "days_since_order": 3}},
            {"fetch_policy": {"policy_docs": [{"text": "7 天无理由退货"}]}},
            {"synthesize": {"final_answer": "您的订单可以退款"}},
        ]

        flow = RefundFlow(
            query="我要退款",
            user_id=1,
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
        )
        events = list(flow.run())

        # 验证流：meta(fetch_order) → meta(decide) → meta(fetch_policy) → meta(synthesize) → token → done
        # 注意：fetch_order 没显式 yield（与 handle_refund_v3 一致，只在 decide/fetch_policy/synthesize yield meta）
        meta_stages = [e[1].get("flow_stage") for e in events if e[0] == "meta"]
        assert "decide" in meta_stages
        assert "fetch_policy" in meta_stages
        assert "synthesize" in meta_stages

        # 验证 decide meta 携带 refundable + reason
        judge_metas = [e[1] for e in events if e[0] == "meta" and e[1].get("flow_stage") == "decide"]
        assert len(judge_metas) == 1
        assert judge_metas[0]["refundable"] is True
        assert "已签收" in judge_metas[0]["reason"]

        # 验证 fetch_policy meta 携带 policy_hits
        policy_metas = [e[1] for e in events if e[0] == "meta" and e[1].get("flow_stage") == "fetch_policy"]
        assert len(policy_metas) == 1
        assert policy_metas[0]["policy_hits"] == 1

        # 验证 final_answer 走 token 事件
        token_events = [e[1] for e in events if e[0] == "token"]
        assert "您的订单可以退款" in token_events

        # 验证 done 事件
        done_events = [e for e in events if e[0] == "done"]
        assert len(done_events) == 1

    @patch("app.services.business_flow.refund_flow.handle_refund_v2")
    @patch("app.services.business_flow.refund_flow.refund_graph_app")
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_langgraph_error_falls_back_to_v2(self, _mock_extract, mock_app, mock_v2):
        """LangGraph 异常 → fallback 到 handle_refund_v2（保险丝）"""
        mock_app.stream.side_effect = Exception("LangGraph 挂了")
        # mock V2 返回一个简单事件流
        def v2_generator(*args, **kwargs):
            yield ("meta", {"intent": "refund_query", "v3_engine": "v2"})
            yield ("token", "V2 fallback 答案")
            yield ("done", {"answer": ""})
        mock_v2.side_effect = v2_generator

        flow = RefundFlow(
            query="我要退款",
            user_id=1,
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
        )
        events = list(flow.run())

        # 验证 V2 被调
        mock_v2.assert_called_once()
        # 验证事件流是 V2 的输出
        meta_events = [e[1] for e in events if e[0] == "meta"]
        assert any(m.get("v3_engine") == "v2" for m in meta_events)


# =============================================================
# 4.5 RefundFlow 自动解析（2026-07-18 改造 · 真实业务场景）
# =============================================================
class TestRefundFlowAutoResolve:
    """RefundFlow 接入 OrderContextResolver 后，无 order_no 不再问用户要订单号。

    真实业务场景：顾客说"我的衣服有问题能退吗"，CS 用系统查顾客订单而非问订单号。
    Resolver 决策：
      - 0 单 → ASK_LOGIN_OR_LIST
      - 1 单 → DIRECT_ANSWER（自动用，绕过询问）
      - N 单 → SHOW_PICKER（yield meta.card，前端 OrderCard list 渲染）

    所有测试 patch ENABLE_ORDER_RESOLVER=True（否则 Resolver 默认 disabled 返 DIRECT_ANSWER，
    effective_order_no 为空，会 fallback 到 LangGraph 触发 DB 连接）。
    """

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_no_order_no_zero_orders_yields_no_order_message(self, _mock_extract, mock_tool):
        """无 order_no + 用户 0 单 → ASK_LOGIN_OR_LIST + '您当前没有订单'"""
        mock_tool.list_user_orders.return_value = []
        mock_tool.get_order_by_no.return_value = None

        flow = RefundFlow(
            query="我要退款",
            user_id=1,
            intent_result={"intent": "refund_query", "entities": {}},
        )
        events = list(flow.run())

        # 第一个 meta 应含 resolver_action=ask_login_or_list
        meta_events = [e[1] for e in events if e[0] == "meta"]
        assert len(meta_events) >= 1
        assert any(m.get("resolver_action") == "ask_login_or_list" for m in meta_events)

        # token 含"没有订单"
        token_events = [e for e in events if e[0] == "token"]
        full_text = "".join(e[1] for e in token_events)
        assert "没有订单" in full_text
        # 关键：不再包含"请提供"
        assert "请提供" not in full_text

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    @patch("app.services.business_flow.refund_flow.refund_graph_app")
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_no_order_no_one_order_uses_direct_answer(self, _mock_extract, mock_app, mock_tool):
        """无 order_no + 用户 1 单 → DIRECT_ANSWER + 走 LangGraph（不询问）"""
        mock_tool.list_user_orders.return_value = [
            {"order_no": "ORD20260718001", "status": "delivered", "total_amount": 299.0, "create_time": "2026-07-15T10:00:00"}
        ]
        mock_tool.get_order_by_no.return_value = None
        # mock LangGraph 走通
        mock_app.stream.return_value = [
            {"decide": {"refundable": True, "reason": "已签收 3 天", "days_since_order": 3}},
            {"synthesize": {"final_answer": "您的订单可以退款"}},
        ]

        flow = RefundFlow(
            query="我想退件衣服",
            user_id=10002,
            intent_result={"intent": "refund_query", "entities": {}},  # 无 order_no
        )
        events = list(flow.run())

        # 验证 LangGraph 被调（说明 effective_order_no 被 Resolver 自动填上）
        mock_app.stream.assert_called_once()
        call_kwargs = mock_app.stream.call_args[0][0]
        assert call_kwargs["order_no"] == "ORD20260718001"
        assert call_kwargs["user_id"] == 10002

        # 验证流中有 decide/synthesize meta（LangGraph 路径被走通）
        meta_stages = [e[1].get("flow_stage") for e in events if e[0] == "meta"]
        assert "decide" in meta_stages
        assert "synthesize" in meta_stages

        # 关键：不再 yield "请提供" token
        token_events = [e for e in events if e[0] == "token"]
        full_text = "".join(e[1] for e in token_events)
        assert "请提供" not in full_text

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_no_order_no_n_orders_yields_picker_card(self, _mock_extract, mock_tool):
        """无 order_no + 用户 N 单 → SHOW_PICKER + meta.card.type='order_list'"""
        mock_tool.list_user_orders.return_value = [
            {"order_no": "ORD20260718001", "status": "delivered", "total_amount": 299.0, "create_time": "2026-07-15T10:00:00"},
            {"order_no": "ORD20260718002", "status": "shipped", "total_amount": 199.0, "create_time": "2026-07-16T10:00:00"},
            {"order_no": "ORD20260718003", "status": "completed", "total_amount": 399.0, "create_time": "2026-07-10T10:00:00"},
        ]
        mock_tool.get_order_by_no.return_value = None

        flow = RefundFlow(
            query="我想退件衣服",
            user_id=10006,  # USER_MULTI_ORDERS
            intent_result={"intent": "refund_query", "entities": {}},
        )
        events = list(flow.run())

        # 第一个 meta 应含 resolver_action=show_picker + card.type=order_list
        meta_events = [e[1] for e in events if e[0] == "meta"]
        picker_metas = [m for m in meta_events if m.get("resolver_action") == "show_picker"]
        assert len(picker_metas) == 1
        assert picker_metas[0]["card"]["type"] == "order_list"
        assert picker_metas[0]["total_orders"] == 3

        # token 应含"请选择"
        token_events = [e for e in events if e[0] == "token"]
        full_text = "".join(e[1] for e in token_events)
        assert "选择" in full_text
        # 不再有"请提供"
        assert "请提供" not in full_text

    @patch.object(settings, "ENABLE_ORDER_RESOLVER", True)
    @patch("app.services.context.order_context_resolver.OrderTool")
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_user_provided_order_no_skips_resolver(self, _mock_extract, mock_tool):
        """有 order_no（用户提供）→ 跳过 Resolver，直接走 LangGraph（兼容旧路径）"""
        mock_tool.list_user_orders.return_value = []  # 即便 0 订单，用户已提供有效 order_no
        mock_tool.get_order_by_no.return_value = {
            "order_no": "ORD20260718999",
            "status": "delivered",
            "total_amount": 199.0,
            "create_time": "2026-07-15T10:00:00",
        }
        # 此场景 Resolver 不会被调，因为 entities.order_no 已提供
        # LangGraph 应走通
        with patch("app.services.business_flow.refund_flow.refund_graph_app") as mock_app:
            mock_app.stream.return_value = [
                {"decide": {"refundable": True, "reason": "已签收 3 天", "days_since_order": 3}},
                {"synthesize": {"final_answer": "OK"}},
            ]

            flow = RefundFlow(
                query="ORD20260718999 啥情况",
                user_id=10002,
                intent_result={"intent": "refund_query", "entities": {"order_no": "ORD20260718999"}},
            )
            events = list(flow.run())
            mock_app.stream.assert_called_once()
            assert mock_app.stream.call_args[0][0]["order_no"] == "ORD20260718999"
class TestCreateBusinessFlow:
    def test_disabled_returns_none_via_helper(self):
        """create_business_flow() 走工厂路径"""
        with patch.object(settings, "ENABLE_BUSINESS_FLOW", False):
            flow = RefundFlow(
                query="x",
                user_id=1,
                intent_result={"intent": "refund_query", "entities": {}},
            )
            # 直接调 factory 即可，无需测 helper（helper 只是 factory 包装）
            assert flow is not None  # 单独的 RefundFlow 实例可以创建
            assert BusinessFlowFactory.create(
                intent="refund_query",
                query="x",
                user_id=1,
                intent_result={"intent": "refund_query", "entities": {}},
            ) is None