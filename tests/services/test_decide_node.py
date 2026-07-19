"""tests/services/test_decide_node.py

M14 V3 decide 节点单测（真实工作流重构配套）

覆盖（SOP §1.3 L2 强制）：
  4 硬规则 + 4 LLM mock + retry + dialog_turn + image_urls 兜底 + 3 P1 = 14 case

P0-1 修订：decide_retry_count 仅统计 LLM 异常；dialog_turn_count 独立计数
P0-2 修订：0 单 → synthesize（不 escalate）
P0-3 修订：fetch_policy 仅 synthesize+policy_needed 时触发

P1-1：高风险关键词硬规则前置（detect_p0_escalate，命中不调 LLM）
P1-2：image_urls 兜底（state 已有凭证 + LLM 要凭证 → 强制 escalate P1）
P1-3：status_zh 在 Resolver 层生成（orders 列表每项带 status_zh）
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")

from app.services.refund_graph import (  # noqa: E402
    decide_node,
    _apply_hard_rules,
    _validate_decide_output,
    _build_decide_prompt,
    _should_fetch_policy,
)


# =============================================================
# 1. 硬规则前置（4 条 · P0-2 修订：0 单降级为 synthesize）
# =============================================================
class TestHardRules:
    """硬规则前置 — 不调 LLM，命中即返回 decide_result"""

    def test_show_picker_returns_need_confirm_order(self):
        """Resolver SHOW_PICKER → need_confirm_order（复用 Resolver 决策）"""
        state = {
            "resolver_result": {"action": "show_picker", "total_orders": 3},
            "history": [],
        }
        result = _apply_hard_rules(state)
        assert result is not None
        assert result["decision"] == "need_confirm_order"
        assert result["confidence"] == 1.0
        assert result["target_order_no"] is None
        assert result["policy_needed"] is False

    def test_zero_orders_returns_synthesize_not_escalate(self):
        """P0-2 修订：Resolver ASK_LOGIN_OR_LIST → synthesize（不打爆人工坐席）"""
        state = {
            "resolver_result": {"action": "ask_login_or_list", "total_orders": 0},
            "history": [],
        }
        result = _apply_hard_rules(state)
        assert result is not None
        assert result["decision"] == "synthesize", \
            "0 单场景必须降级为 synthesize（不打爆人工坐席）"
        assert result["confidence"] == 1.0
        assert result["policy_needed"] is False
        assert "暂无订单" in result["reply_key_points"][0]
        assert "确认下单账号" in "".join(result["reply_key_points"])

    def test_not_found_returns_escalate_p2(self):
        """Resolver NOT_FOUND（订单不存在/越权）→ escalate P2"""
        state = {
            "resolver_result": {"action": "not_found"},
            "history": [],
        }
        result = _apply_hard_rules(state)
        assert result is not None
        assert result["decision"] == "escalate"
        assert result["escalate"]["enabled"] is True
        assert result["escalate"]["priority"] == "P2"
        assert result["escalate"]["category"] == "复杂场景"

    def test_history_commitment_returns_escalate_p1(self):
        """硬规则 4：历史承诺匹配 → escalate P1（履约承诺）"""
        state = {
            "resolver_result": {"action": "direct_answer"},
            "history": [
                {"role": "assistant", "content": "好的，已为您转接人工客服"},
            ],
        }
        result = _apply_hard_rules(state)
        assert result is not None
        assert result["decision"] == "escalate"
        assert result["escalate"]["priority"] == "P1"
        assert result["escalate"]["category"] == "用户要求"


# =============================================================
# 2. LLM 决策（4 类 decision，mock LLM 返回）
# =============================================================
class TestLLMDecision:
    """decide 节点调 LLM → 4 类 decision + 校验 + 置信度兜底"""

    def _make_state(self, **overrides):
        """基础 state 模板（硬规则未命中，DIRECT_ANSWER 场景）"""
        base = {
            "resolver_result": {"action": "direct_answer", "total_orders": 1},
            "orders": [{
                "order_no": "O20260718001",
                "sku_name": "智能手机",
                "status_zh": "已签收",
                "sign_time": "2026-07-18",
                "amount": 3999.00,
                "is_customized": False,
            }],
            "refundable": True,
            "reason": "签收 1 天在 7 天时效内",
            "days_since_order": 1,
            "status_zh": "已签收",
            "query": "我昨天收到的手机能退吗",
            "history": [],
            "decide_retry_count": 0,
            "dialog_turn_count": 0,
            "image_urls": [],
        }
        base.update(overrides)
        return base

    def test_synthesize_decision(self):
        """LLM 返回 decision=synthesize → 直接采纳"""
        llm_output = {
            "decision": "synthesize",
            "confidence": 0.95,
            "target_order_no": "O20260718001",
            "reason": "标准 7 天无理由退款",
            "escalate": {"enabled": False},
            "need_info": {"enabled": False},
            "reply_key_points": ["确认订单在 7 天时效内", "告知退款入口"],
            "policy_needed": False,
        }
        with patch("app.services.refund_graph.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {"reply": str(llm_output).replace("'", '"')}
            result = decide_node(self._make_state())
            assert result["decide_result"]["decision"] == "synthesize"
            assert result["decide_result"]["target_order_no"] == "O20260718001"

    def test_need_more_info_decision(self):
        """LLM 返回 decision=need_more_info → 采纳"""
        llm_output = {
            "decision": "need_more_info",
            "confidence": 0.92,
            "target_order_no": "O20260718001",
            "reason": "质量问题但未说明现象和凭证",
            "escalate": {"enabled": False},
            "need_info": {"enabled": True, "fields": ["故障现象", "凭证", "故障视频"]},
            "reply_key_points": [],
            "policy_needed": False,
        }
        with patch("app.services.refund_graph.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {"reply": str(llm_output).replace("'", '"')}
            result = decide_node(self._make_state())
            assert result["decide_result"]["decision"] == "need_more_info"
            assert "凭证" in result["decide_result"]["need_info"]["fields"]

    def test_need_confirm_order_decision(self):
        """LLM 返回 decision=need_confirm_order → 采纳（即使 Resolver 是 DIRECT_ANSWER）"""
        llm_output = {
            "decision": "need_confirm_order",
            "confidence": 0.93,
            "target_order_no": None,
            "reason": "用户提到两件衣服但列表只有 1 件 T 恤",
            "escalate": {"enabled": False},
            "need_info": {"enabled": False},
            "reply_key_points": ["请确认指哪件衣服"],
            "policy_needed": False,
        }
        with patch("app.services.refund_graph.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {"reply": str(llm_output).replace("'", '"')}
            result = decide_node(self._make_state())
            assert result["decide_result"]["decision"] == "need_confirm_order"
            assert result["decide_result"]["target_order_no"] is None

    def test_escalate_decision_p0(self):
        """LLM 返回 decision=escalate P0 → 采纳"""
        llm_output = {
            "decision": "escalate",
            "confidence": 0.99,
            "target_order_no": "O20260718001",
            "reason": "用户威胁投诉 12315",
            "escalate": {
                "enabled": True, "priority": "P0", "category": "投诉",
                "handoff_summary": "用户威胁投诉 12315，情绪激动",
            },
            "need_info": {"enabled": False},
            "reply_key_points": [],
            "policy_needed": False,
        }
        with patch("app.services.refund_graph.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {"reply": str(llm_output).replace("'", '"')}
            result = decide_node(self._make_state())
            assert result["decide_result"]["decision"] == "escalate"
            assert result["decide_result"]["escalate"]["priority"] == "P0"


# =============================================================
# 3. 校验 + 兜底（P0-1 修订：retry/dialog 分离）
# =============================================================
class TestValidationAndFallback:
    """_validate_decide_output + 置信度兜底 + retry 兜底"""

    def test_low_confidence_auto_escalate(self):
        """confidence < 0.7 → 自动降级 escalate P2"""
        output = {
            "decision": "synthesize",
            "confidence": 0.55,  # 低于阈值 0.7
            "target_order_no": "O001",
            "reason": "AI 不确定",
            "escalate": {"enabled": False},
            "need_info": {"enabled": False},
            "reply_key_points": [],
            "policy_needed": False,
        }
        valid, validated = _validate_decide_output(output)
        assert valid is True
        assert validated["decision"] == "escalate", "低置信度必须降级 escalate"
        assert validated["escalate"]["priority"] == "P2"

    def test_invalid_decision_enum_rejected(self):
        """decision 枚举非法 → 校验失败 → 走兜底"""
        output = {
            "decision": "invalid_xxx",
            "confidence": 0.9,
            "target_order_no": None,
            "reason": "",
            "escalate": {"enabled": False},
            "need_info": {"enabled": False},
            "reply_key_points": [],
            "policy_needed": False,
        }
        valid, validated = _validate_decide_output(output)
        assert valid is False
        assert validated["decision"] == "escalate", "非法枚举 → 兜底 escalate P2"

    def test_synthesize_without_target_order_rejected(self):
        """decision=synthesize 但 target_order_no 空 → 校验失败"""
        output = {
            "decision": "synthesize",
            "confidence": 0.9,
            "target_order_no": None,
            "reason": "",
            "escalate": {"enabled": False},
            "need_info": {"enabled": False},
            "reply_key_points": [],
            "policy_needed": False,
        }
        valid, validated = _validate_decide_output(output)
        assert valid is False


# =============================================================
# 4. 重试计数（P0-1 核心修订：retry 和 dialog 分离）
# =============================================================
class TestRetryCounting:
    """P0-1 修订：decide_retry_count 仅 LLM 异常累计；dialog_turn_count 独立"""

    def test_user_dialog_turn_not_counted_as_retry(self):
        """用户对话（dialog_turn_count=3）不应触发 decide_retry_count"""
        # 直接构造 state：retry_count=0, dialog_turn=3, LLM 成功
        llm_output = {
            "decision": "synthesize",
            "confidence": 0.95,
            "target_order_no": "O001",
            "reason": "",
            "escalate": {"enabled": False},
            "need_info": {"enabled": False},
            "reply_key_points": [],
            "policy_needed": False,
        }
        state = {
            "resolver_result": {"action": "direct_answer"},
            "orders": [],
            "refundable": True, "reason": "", "days_since_order": 1, "status_zh": "已签收",
            "query": "test", "history": [], "decide_retry_count": 0,
            "dialog_turn_count": 3,  # 用户对话 3 轮，不应影响 retry
            "image_urls": [],
        }
        with patch("app.services.refund_graph.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {"reply": str(llm_output).replace("'", '"')}
            result = decide_node(state)
            # retry_count 仍是 0（用户对话不计数）
            assert result.get("decide_retry_count", 0) == 0, \
                "用户对话不应触发 retry_count"

    def test_llm_format_error_increments_retry(self):
        """LLM 返回非 JSON → retry_count + 1"""
        state = {
            "resolver_result": {"action": "direct_answer"},
            "orders": [],
            "refundable": True, "reason": "", "days_since_order": 1, "status_zh": "已签收",
            "query": "test", "history": [], "decide_retry_count": 0,
            "dialog_turn_count": 0,
            "image_urls": [],
        }
        with patch("app.services.refund_graph.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {"reply": "这不是合法 JSON"}
            result = decide_node(state)
            # retry_count 应为 1
            assert result.get("decide_retry_count", 0) == 1

    def test_retry_overflow_escalates_p2(self):
        """连续 3 次 LLM 失败 → 自动 escalate P2 'AI 多次异常'"""
        state = {
            "resolver_result": {"action": "direct_answer"},
            "orders": [],
            "refundable": True, "reason": "", "days_since_order": 1, "status_zh": "已签收",
            "query": "test", "history": [], "decide_retry_count": 2,  # 即将达到阈值
            "dialog_turn_count": 0,
            "image_urls": [],
        }
        with patch("app.services.refund_graph.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {"reply": "not json"}
            result = decide_node(state)
            assert result["decide_result"]["decision"] == "escalate"
            assert result["decide_result"]["escalate"]["priority"] == "P2"


# =============================================================
# 5. P1 优化（3 处）
# =============================================================
class TestP1Optimizations:
    """P1-1/2/3 硬规则补充"""

    def test_p1_1_p0_keyword_skips_llm(self):
        """P1-1：query 含 '12315' → detect_p0_escalate 命中 → decide_node 不调 LLM"""
        from app.services.escalation_service import detect_p0_escalate

        result = detect_p0_escalate("我要投诉 12315")
        assert result is not None
        category, keyword = result
        assert category == "complaint"
        assert keyword == "12315"

        # decide_node 在 query 含 P0 关键词时由 chat.py 上层拦截，不进 LangGraph
        # 这里只验证 detect_p0_escalate 本身
        # 注意：P0 关键词拦截放在 chat.py，不在 decide 节点（避免 LLM 调用）

    def test_p1_2_image_urls_override_need_more_info(self):
        """P1-2：state.image_urls 非空 + LLM 要凭证 → 强制 escalate P1"""
        llm_output = {
            "decision": "need_more_info",
            "confidence": 0.9,
            "target_order_no": "O001",
            "reason": "需要凭证",
            "escalate": {"enabled": False},
            "need_info": {"enabled": True, "fields": ["凭证照片", "故障视频"]},
            "reply_key_points": [],
            "policy_needed": False,
        }
        state = {
            "resolver_result": {"action": "direct_answer"},
            "orders": [], "refundable": True, "reason": "",
            "days_since_order": 1, "status_zh": "已签收",
            "query": "质量问题", "history": [],
            "decide_retry_count": 0, "dialog_turn_count": 0,
            "image_urls": ["http://img.example.com/x.jpg"],  # 用户已上传凭证
        }
        with patch("app.services.refund_graph.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {"reply": str(llm_output).replace("'", '"')}
            result = decide_node(state)
            # image_urls 非空 + LLM 要凭证 → 强制 escalate P1
            assert result["decide_result"]["decision"] == "escalate"
            assert result["decide_result"]["escalate"]["priority"] == "P1"
            assert result["decide_result"]["escalate"]["category"] == "质量问题"

    def test_p1_3_status_zh_in_resolver_orders(self):
        """P1-3：Resolver 阶段 orders 列表每项带 status_zh"""
        from app.services.context.order_context_resolver import (
            OrderContextResolver,
            OrderResolverAction,
        )

        # mock OrderTool.list_user_orders 返回订单
        with patch("app.services.context.order_context_resolver.OrderTool") as mock_tool:
            mock_tool.list_user_orders.return_value = [
                {"order_no": "O001", "status": "delivered", "sku_name": "手机"},
                {"order_no": "O002", "status": "shipped", "sku_name": "耳机"},
            ]
            from app.services.context.context_service import ConversationContext
            resolver = OrderContextResolver()
            ctx = ConversationContext(session_id="s1", user_id=42)
            result = resolver.resolve(42, "order_query", {}, ctx)

            # orders 列表每项应带 status_zh
            for order in result.candidate_orders:
                assert "status_zh" in order, \
                    f"order {order['order_no']} 应带 status_zh"
                assert order["status_zh"] in {"待支付", "已支付", "运输中", "已签收", "已完成", "已退款"}


# =============================================================
# 6. fetch_policy 条件触发（P0-3）
# =============================================================
class TestFetchPolicyTrigger:
    """P0-3：fetch_policy 仅 synthesize+policy_needed 时触发"""

    def test_synthesize_with_policy_triggers_fetch(self):
        """decision=synthesize + policy_needed=True → fetch_policy"""
        state = {
            "decide_result": {
                "decision": "synthesize",
                "policy_needed": True,
            }
        }
        result = _should_fetch_policy(state)
        assert result == "fetch_policy"

    def test_synthesize_without_policy_skips(self):
        """decision=synthesize + policy_needed=False → 直接 synthesize（跳过）"""
        state = {
            "decide_result": {
                "decision": "synthesize",
                "policy_needed": False,
            }
        }
        result = _should_fetch_policy(state)
        assert result == "synthesize"

    def test_non_synthesize_skips_fetch_policy(self):
        """decision=need_more_info / need_confirm_order / escalate → 跳过 fetch_policy"""
        for decision in ("need_more_info", "need_confirm_order", "escalate"):
            state = {
                "decide_result": {
                    "decision": decision,
                    "policy_needed": True,
                }
            }
            result = _should_fetch_policy(state)
            assert result == "synthesize", f"{decision} 应跳过 fetch_policy"