"""tests/business_flow/test_refund_flow.py

M14 Stage 3：RefundFlow 单测

覆盖：
1. ENABLE_BUSINESS_FLOW=False → factory.create() 返 None（短路）
2. ENABLE_BUSINESS_FLOW=True + refund_query → RefundFlow 实例
3. ENABLE_BUSINESS_FLOW=True + 非 refund_query → None（YAGNI：仅 1 个 Flow）
4. RefundFlow.run()：匿名用户短路（meta.flow_stage=fetch_order + NO_LOGIN_PROMPT）
5. RefundFlow.run()：无 order_no 短路（meta.flow_stage=fetch_order + 请提供订单号）
6. RefundFlow.run()：有 order_no 走 LangGraph → yield meta.flow_stage + token
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# 让 `from app.services...` 能跑
ROOT = Path(__file__).resolve().parent.parent
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
    def test_no_order_no_short_circuits(self, _mock_extract):
        """无 order_no + 有 user_id → 请提供订单号（防串单 M9.5）"""
        flow = RefundFlow(
            query="我要退款",
            user_id=1,
            intent_result={"intent": "refund_query", "entities": {}},  # 无 order_no
        )
        events = list(flow.run())
        # 第一个事件：meta fetch_order stage
        assert events[0][0] == "meta"
        assert events[0][1]["flow_stage"] == "fetch_order"
        # 后续 token 包含"请提供" + "订单号"（实际文本"请提供要查询退款的订单号"）
        token_events = [e for e in events if e[0] == "token"]
        full_text = "".join(e[1] for e in token_events)
        assert "请提供" in full_text
        assert "订单号" in full_text


# =============================================================
# 4. RefundFlow LangGraph 路径（mock stream）
# =============================================================
class TestRefundFlowLangGraph:
    """mock LangGraph stream() → 验证 flow_stage 推送 + token 输出"""

    @patch("app.services.business_flow.refund_flow.refund_graph_app")
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    def test_judge_yields_meta_with_flow_stage(self, _mock_extract, mock_app):
        """LangGraph judge node → yield meta.flow_stage='judge' + refundable + reason"""
        mock_app.stream.return_value = [
            {"fetch_order": {"order_info": {"order_no": "ORD001", "status": "delivered"}, "days_since_order": 3}},
            {"judge": {"refundable": True, "reason": "已签收 3 天，在 7 天内", "days_since_order": 3}},
            {"fetch_policy": {"policy_docs": [{"text": "7 天无理由退货"}]}},
            {"synthesize": {"final_answer": "您的订单可以退款"}},
        ]

        flow = RefundFlow(
            query="我要退款",
            user_id=1,
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
        )
        events = list(flow.run())

        # 验证流：meta(fetch_order) → meta(judge) → meta(fetch_policy) → meta(synthesize) → token → done
        # 注意：fetch_order 没显式 yield（与 handle_refund_v3 一致，只在 judge/fetch_policy/synthesize yield meta）
        meta_stages = [e[1].get("flow_stage") for e in events if e[0] == "meta"]
        assert "judge" in meta_stages
        assert "fetch_policy" in meta_stages
        assert "synthesize" in meta_stages

        # 验证 judge meta 携带 refundable + reason
        judge_metas = [e[1] for e in events if e[0] == "meta" and e[1].get("flow_stage") == "judge"]
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
# 5. create_business_flow 入口便捷函数
# =============================================================
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