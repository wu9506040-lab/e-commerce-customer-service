"""
退款流程 LangGraph 版 - 单元测试

策略：mock 外部依赖（OrderTool / PolicyService / qwen_chat），
       验证 Node 函数逻辑 + 图路径分支。

不依赖 MySQL / Qdrant / LLM API，可独立运行。
"""
from unittest.mock import patch, MagicMock

import pytest


# ============ 辅助：构造 mock 订单 ============

def make_order(status="delivered", days_ago=3):
    """生成 mock 订单（create_time 是 N 天前）"""
    import datetime
    create_time = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
    return {
        "order_no": "ORD001",
        "status": status,
        "total_amount": 599.0,
        "create_time": create_time,
    }


# ============ 测试 Node 函数（单元） ============

class TestFetchOrder:
    """Node 1: fetch_order"""

    @patch("app.services.refund_graph.OrderTool")
    def test_order_exists(self, mock_tool):
        from app.services.refund_graph import fetch_order
        mock_tool.get_order_by_no.return_value = make_order(days_ago=3)

        state = {"user_id": 42, "order_no": "ORD001"}
        result = fetch_order(state)

        # delivered 订单按「签收日」算：create_time + 2 天 = 签收日（与 OrderTool.get_logistics 一致）
        # days_ago=3 → 签收是 1 天前
        assert result["days_since_order"] == 1
        assert result["order_info"]["status"] == "delivered"
        mock_tool.get_order_by_no.assert_called_once_with(42, "ORD001")

    @patch("app.services.refund_graph.OrderTool")
    def test_order_not_found(self, mock_tool):
        from app.services.refund_graph import fetch_order
        mock_tool.get_order_by_no.return_value = None

        state = {"user_id": 42, "order_no": "ORDXXX"}
        result = fetch_order(state)

        assert result["order_info"] == {}
        # 订单不存在 → 用 0 兜底（取代老 sentinel 999，避免 magic number）
        assert result["days_since_order"] == 0


class TestJudgeBasic:
    """Node 2: judge_basic_refundable"""

    def test_delivered_within_7_days(self):
        from app.services.refund_graph import judge_basic_refundable
        state = {"order_info": make_order("delivered", days_ago=3), "days_since_order": 3}
        result = judge_basic_refundable(state)
        assert result["refundable"] is True
        assert "7 天" in result["reason"]

    def test_delivered_over_7_days(self):
        from app.services.refund_graph import judge_basic_refundable
        state = {"order_info": make_order("delivered", days_ago=10), "days_since_order": 10}
        result = judge_basic_refundable(state)
        assert result["refundable"] is False
        assert "超过" in result["reason"]

    def test_already_refunded(self):
        from app.services.refund_graph import judge_basic_refundable
        state = {"order_info": make_order("refunded", days_ago=3), "days_since_order": 3}
        result = judge_basic_refundable(state)
        assert result["refundable"] is False
        assert "已退款" in result["reason"]

    def test_order_not_found(self):
        from app.services.refund_graph import judge_basic_refundable
        state = {"order_info": {}, "days_since_order": 999}
        result = judge_basic_refundable(state)
        assert result["refundable"] is False
        assert "不存在" in result["reason"]


class TestCheckProof:
    """Node 4: check_user_proof"""

    def test_quality_issue_no_proof(self):
        from app.services.refund_graph import check_user_proof
        state = {"query": "质量有问题能退吗", "user_proof": {}}
        result = check_user_proof(state)
        assert result["escalate_to_human"] is True

    def test_quality_issue_with_proof(self):
        from app.services.refund_graph import check_user_proof
        state = {"query": "质量有问题能退吗", "user_proof": {"image": "xxx"}}
        result = check_user_proof(state)
        assert result["escalate_to_human"] is False

    def test_non_quality_issue(self):
        from app.services.refund_graph import check_user_proof
        state = {"query": "三天前买的能退吗", "user_proof": {}}
        result = check_user_proof(state)
        assert result["escalate_to_human"] is False


class TestEscalate:
    """Node 5: escalate_to_human"""

    def test_escalate_message(self):
        from app.services.refund_graph import escalate_to_human
        state = {"reason": "需提供质量问题凭证"}
        result = escalate_to_human(state)
        assert "人工" in result["final_answer"]
        assert "凭证" in result["final_answer"]


# ============ 测试图路径（端到端，mock 所有外部） ============

class TestRefundGraph:
    """测试 StateGraph 的路径分支"""

    @patch("app.services.refund_graph.get_llm_provider")
    @patch("app.services.refund_graph.PolicyService")
    @patch("app.services.refund_graph.OrderTool")
    def test_path1_refundable_with_policy(self, mock_tool, mock_policy, mock_provider):
        """路径 1：可退 + 查政策 + 合成答案"""
        mock_tool.get_order_by_no.return_value = make_order("delivered", days_ago=3)
        mock_policy.search_policy.return_value = [
            {"text": "七天无理由退货规则..."},
            {"text": "已发货商品退款流程..."},
        ]
        mock_llm = mock_provider.return_value.chat
        mock_llm.return_value = {"reply": "您的订单符合 7 天无理由退货条件，可以退。"}

        from app.services.refund_graph import refund_graph_app
        result = refund_graph_app.invoke({
            "user_id": 42,
            "order_no": "ORD001",
            "query": "三天前买的能退吗",
        })

        assert "7 天" in result["final_answer"]
        assert mock_tool.get_order_by_no.called
        assert mock_policy.search_policy.called
        assert mock_llm.called

    @patch("app.services.refund_graph.get_llm_provider")
    @patch("app.services.refund_graph.OrderTool")
    def test_path2_quality_issue_escalate(self, mock_tool, mock_provider):
        """路径 2：质量问题无凭证 → 升级人工（不调 LLM）"""
        mock_tool.get_order_by_no.return_value = make_order("delivered", days_ago=3)

        from app.services.refund_graph import refund_graph_app
        result = refund_graph_app.invoke({
            "user_id": 42,
            "order_no": "ORD001",
            "query": "质量有问题能退吗",
            "user_proof": {},
        })

        assert "人工" in result["final_answer"]
        # 升级路径不调 LLM
        assert not mock_provider.return_value.chat.called

    @patch("app.services.refund_graph.get_llm_provider")
    @patch("app.services.refund_graph.OrderTool")
    def test_path3_over_7_days(self, mock_tool, mock_provider):
        """路径 3：超过 7 天 → 不可退 → 直接合成答案"""
        mock_tool.get_order_by_no.return_value = make_order("delivered", days_ago=15)
        mock_llm = mock_provider.return_value.chat
        mock_llm.return_value = {"reply": "您的订单已超过 7 天，不符合退款条件。"}

        from app.services.refund_graph import refund_graph_app
        result = refund_graph_app.invoke({
            "user_id": 42,
            "order_no": "ORD002",
            "query": "能退吗",
        })

        assert "超过" in result["final_answer"] or "不符合" in result["final_answer"]
        # 不可退路径不查政策
        assert mock_llm.called

    @patch("app.services.refund_graph.get_llm_provider")
    @patch("app.services.refund_graph.OrderTool")
    def test_path4_already_refunded(self, mock_tool, mock_provider):
        """路径 4：已退款 → 直接合成答案"""
        mock_tool.get_order_by_no.return_value = make_order("refunded", days_ago=3)
        mock_llm = mock_provider.return_value.chat
        mock_llm.return_value = {"reply": "该订单已退款。"}

        from app.services.refund_graph import refund_graph_app
        result = refund_graph_app.invoke({
            "user_id": 42,
            "order_no": "ORD003",
            "query": "能退吗",
        })

        assert "已退款" in result["final_answer"]


# ============ 测试图构建 ============

class TestGraphStructure:
    def test_graph_has_6_business_nodes(self):
        """图应该有 6 个业务 Node（不含 __start__）"""
        from app.services.refund_graph import refund_graph_app
        expected = {"fetch_order", "judge", "fetch_policy", "check_proof", "escalate", "synthesize"}
        actual = set(refund_graph_app.nodes.keys()) - {"__start__"}
        assert actual == expected

    def test_graph_compiles(self):
        """图能正常 compile（单例已 build 过）"""
        from app.services.refund_graph import refund_graph_app
        assert refund_graph_app is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])