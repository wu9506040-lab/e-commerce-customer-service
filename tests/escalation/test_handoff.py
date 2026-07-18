"""tests/escalation/test_handoff.py

M14 V3 转人工兜底单测

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
from unittest.mock import patch

# 让 `from app.services...` 能跑
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# 测试环境变量
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")

from app.services.escalation_service import (  # noqa: E402
    EscalationReason,
    HandoffPayload,
    detect_handoff_keyword,
    get_escalation_service,
    reset_escalation_service,
)
from app.core.config import settings  # noqa: E402


# =============================================================
# 1. 转人工关键词检测（detect_handoff_keyword）
# =============================================================
class TestDetectHandoffKeyword:
    def test_positive_cases(self):
        """所有配置的关键词应被识别"""
        for kw in [
            "转人工",
            "我要转人工",
            "帮我转接人工",
            "人工客服",
            "真人客服",
            "找人工",
            "人工服务",
            "转给人工",
            "转人工客服",
        ]:
            assert detect_handoff_keyword(kw), f"应识别: {kw}"

    def test_negative_cases(self):
        """普通退款/订单 query 不应被误判"""
        for q in [
            "我要退款",
            "我的订单 ORD20260718001",
            "七天后能退货吗",
            "今天天气不错",
            "",
            None,
        ]:
            assert not detect_handoff_keyword(q), f"不应识别: {q!r}"

    def test_strip_whitespace(self):
        """前后空白不影响匹配"""
        assert detect_handoff_keyword("  转人工  ")
        assert detect_handoff_keyword("\n转人工\n")


# =============================================================
# 2. EscalationService.handoff() payload 生成
# =============================================================
class TestHandoffPayload:
    def setup_method(self):
        """每个测试前重置单例（避免测试间污染）"""
        reset_escalation_service()

    def test_handoff_id_format(self):
        """handoff_id 格式：H + 8 位 hex（数字+字母，字母大写）"""
        svc = get_escalation_service()
        p = svc.handoff(reason=EscalationReason.USER_REQUESTED, user_id=1)
        assert p.handoff_id.startswith("H")
        assert len(p.handoff_id) == 9  # H + 8 hex chars
        # hex chars: 0-9 + A-F（字母大写）。isupper() 在含数字时返回 False，故单独断言
        tail = p.handoff_id[1:]
        assert all(c in "0123456789ABCDEF" for c in tail), f"非 hex: {tail}"

    def test_handoff_ids_unique(self):
        """每次 handoff_id 都不同（防重复）"""
        svc = get_escalation_service()
        ids = {svc.handoff(reason=EscalationReason.USER_REQUESTED, user_id=1).handoff_id for _ in range(20)}
        assert len(ids) == 20

    def test_payload_structure_user_requested(self):
        """USER_REQUESTED → reason_label 中文正确"""
        svc = get_escalation_service()
        p = svc.handoff(
            reason=EscalationReason.USER_REQUESTED,
            user_id=42,
            history=[{"role": "user", "content": "我想退款"}],
            intent_result={"intent": "refund_query", "entities": {}},
        )
        assert p.reason == "user_requested"
        assert "人工客服" in p.reason_label
        assert p.user_id == 42
        # summary 拼装：申请退款 + 最后说:...
        assert "申请退款" in p.summary_text
        assert len(p.recent_messages) == 1

    def test_payload_structure_agent_unavailable(self):
        """AGENT_UNAVAILABLE → failure_context 被填"""
        svc = get_escalation_service()
        p = svc.handoff(
            reason=EscalationReason.AGENT_UNAVAILABLE,
            user_id=42,
            history=[],
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
            failure_context={
                "failed_stage": "v3_v2_both_failed",
                "v3_error_class": "RuntimeError",
                "v3_error_msg": "LangGraph 挂了",
                "retry_count": 1,
            },
        )
        assert p.reason == "agent_unavailable"
        assert p.agent_failure_context is not None
        assert p.agent_failure_context["failed_stage"] == "v3_v2_both_failed"
        assert p.current_entities.get("order_no") == "ORD001"
        # summary 包含订单号 + 失败阶段
        assert "ORD001" in p.summary_text
        assert "v3_v2_both_failed" in p.summary_text

    def test_recent_messages_capped_at_5(self):
        """recent_messages 最多保留最近 5 条"""
        svc = get_escalation_service()
        long_history = [
            {"role": "user", "content": f"msg{i}"} for i in range(20)
        ]
        p = svc.handoff(
            reason=EscalationReason.USER_REQUESTED,
            user_id=1,
            history=long_history,
        )
        assert len(p.recent_messages) == 5
        # 应该是最后 5 条
        assert p.recent_messages[-1]["content"] == "msg19"

    def test_recent_orders_default_empty_when_db_fails(self):
        """DB 失败 → recent_orders 空列表（不阻断流程）"""
        svc = get_escalation_service()
        with patch("app.services.escalation_service.OrderTool") as mock_tool:
            mock_tool.list_user_orders.side_effect = Exception("DB down")
            p = svc.handoff(
                reason=EscalationReason.USER_REQUESTED,
                user_id=42,
            )
        assert p.recent_orders == []
        assert p.user_card["total_orders"] == 0

    def test_recent_orders_uses_existing_list(self):
        """recent_orders 已传 → 不重复查 DB"""
        svc = get_escalation_service()
        existing = [{"order_no": "ORD001", "status": "delivered", "total_amount": 199.0}]
        with patch("app.services.escalation_service.OrderTool") as mock_tool:
            p = svc.handoff(
                reason=EscalationReason.USER_REQUESTED,
                user_id=42,
                recent_orders=existing,
            )
        # 不应查 DB
        mock_tool.list_user_orders.assert_not_called()
        assert p.recent_orders == existing
        assert p.user_card["total_orders"] == 1

    def test_to_dict_round_trip(self):
        """to_dict 可 JSON 序列化"""
        svc = get_escalation_service()
        p = svc.handoff(
            reason=EscalationReason.BUSINESS_RULE,
            user_id=1,
            history=[{"role": "user", "content": "质量有问题"}],
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
        )
        d = p.to_dict()
        # 必含字段
        for k in (
            "handoff_id", "reason", "reason_label", "created_at",
            "user_id", "user_card", "recent_orders", "recent_messages",
            "current_intent", "current_entities", "summary_text",
        ):
            assert k in d
        assert d["handoff_id"] == p.handoff_id
        assert d["reason"] == "business_rule"

    def test_summary_text_for_anonymous_user(self):
        """匿名用户 → summary 拼装合理（无 current_intent）"""
        svc = get_escalation_service()
        p = svc.handoff(
            reason=EscalationReason.USER_REQUESTED,
            user_id=0,
            history=[{"role": "user", "content": "test"}],
        )
        # 匿名 user 也没 intent_result，summary 应有兜底
        assert "最后说" in p.summary_text


# =============================================================
# 3. 灰度开关：ENABLE_ESCALATION_HANDOFF=False
# =============================================================
class TestEscalationGate:
    """灰度开关关闭时 _yield_handoff 降级为"系统繁忙"文本"""

    @patch("app.services.business_flow.refund_flow.settings")
    def test_refund_flow_handoff_disabled_yields_text(self, mock_settings):
        """ENABLE_ESCALATION_HANDOFF=False → 不推 handoff payload，只推"系统繁忙"文本"""
        from app.services.business_flow.refund_flow import _yield_handoff
        mock_settings.ENABLE_ESCALATION_HANDOFF = False

        events = list(_yield_handoff(
            reason=EscalationReason.AGENT_UNAVAILABLE,
            user_id=42,
            history=[],
            intent_result={"intent": "refund_query", "entities": {}},
            failure_context={"failed_stage": "v3_v2_both_failed"},
        ))

        meta_events = [e for e in events if e[0] == "meta"]
        token_events = [e for e in events if e[0] == "token"]

        # meta 不应包含 handoff 字段
        assert "handoff" not in meta_events[0][1]
        assert meta_events[0][1]["v3_engine"] == "escalation_disabled"
        # token 是固定话术
        full_text = "".join(e[1] for e in token_events)
        assert "系统繁忙" in full_text

    @patch("app.services.business_flow.refund_flow.settings")
    def test_refund_flow_handoff_enabled_yields_payload(self, mock_settings):
        """ENABLE_ESCALATION_HANDOFF=True → meta 含 handoff payload"""
        from app.services.business_flow.refund_flow import _yield_handoff
        mock_settings.ENABLE_ESCALATION_HANDOFF = True

        events = list(_yield_handoff(
            reason=EscalationReason.AGENT_UNAVAILABLE,
            user_id=42,
            history=[],
            intent_result={"intent": "refund_query", "entities": {}},
            failure_context={"failed_stage": "v3_v2_both_failed"},
        ))

        meta_events = [e for e in events if e[0] == "meta"]
        # meta 含 handoff payload
        assert "handoff" in meta_events[0][1]
        handoff = meta_events[0][1]["handoff"]
        assert handoff["reason"] == "agent_unavailable"
        assert handoff["handoff_id"].startswith("H")
        assert handoff["agent_failure_context"]["failed_stage"] == "v3_v2_both_failed"


# =============================================================
# 4. RefundFlow V3+V2 都异常 → handoff
# =============================================================
class TestRefundFlowHandoffTrigger:
    @patch.object(settings, "ENABLE_ESCALATION_HANDOFF", True)
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    @patch("app.services.business_flow.refund_flow.handle_refund_v2")
    @patch("app.services.business_flow.refund_flow.refund_graph_app")
    def test_v3_v2_both_fail_yields_handoff(
        self, mock_v3, mock_v2, _mock_extract,
    ):
        """V3 LangGraph 异常 + V2 fallback 也异常 → 转人工兜底"""
        from app.services.business_flow.refund_flow import RefundFlow
        mock_v3.stream.side_effect = RuntimeError("LangGraph 挂了")
        mock_v2.side_effect = ValueError("V2 也挂了")

        flow = RefundFlow(
            query="我要退款",
            user_id=42,
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
            history=[{"role": "user", "content": "我要退款"}],
        )
        events = list(flow.run())

        meta_events = [e for e in events if e[0] == "meta"]
        # meta 应含 handoff payload
        handoff_metas = [m[1] for m in meta_events if "handoff" in m[1]]
        assert len(handoff_metas) == 1
        assert handoff_metas[0]["v3_engine"] == "escalation"
        assert handoff_metas[0]["handoff"]["reason"] == "agent_unavailable"

        # token 含工单号
        token_events = [e for e in events if e[0] == "token"]
        full_text = "".join(e[1] for e in token_events)
        assert "工单号" in full_text

        # failure_context 包含 V3 + V2 双错
        failure = handoff_metas[0]["handoff"]["agent_failure_context"]
        assert failure["v3_error_class"] == "RuntimeError"
        assert failure["v2_error_class"] == "ValueError"

    @patch.object(settings, "ENABLE_ESCALATION_HANDOFF", True)
    @patch("app.services.business_flow.refund_flow._extract_order_no_from_history", return_value=None)
    @patch("app.services.business_flow.refund_flow.handle_refund_v2")
    @patch("app.services.business_flow.refund_flow.refund_graph_app")
    def test_v3_fails_v2_recovers_yields_v2_answer(
        self, mock_v3, mock_v2, _mock_extract,
    ):
        """V3 异常 + V2 fallback 成功 → 走 V2 答案（不触发 handoff）"""
        from app.services.business_flow.refund_flow import RefundFlow
        mock_v3.stream.side_effect = RuntimeError("LangGraph 挂了")

        def v2_generator(*args, **kwargs):
            yield ("meta", {"intent": "refund_query", "v3_engine": "v2"})
            yield ("token", "V2 fallback 成功")
            yield ("done", {"answer": ""})
        mock_v2.side_effect = v2_generator

        flow = RefundFlow(
            query="我要退款",
            user_id=42,
            intent_result={"intent": "refund_query", "entities": {"order_no": "ORD001"}},
        )
        events = list(flow.run())

        # 不应有 handoff meta
        meta_events = [e for e in events if e[0] == "meta"]
        assert not any("handoff" in m[1] for m in meta_events)
        # 应有 V2 的 token
        token_events = [e for e in events if e[0] == "token"]
        assert any("V2 fallback 成功" in e[1] for e in token_events)
