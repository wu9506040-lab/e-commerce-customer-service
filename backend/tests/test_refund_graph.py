"""
退款流程 LangGraph 版 - 图结构 + 端到端路径测试（M14 V3 重构后）

V3 重构（2026-07-19）：6 节点 → 4 节点。
- 旧：fetch_order → judge → fetch_policy → check_proof → escalate/synthesize
- 新：decide → {fetch_policy → synthesize | synthesize | escalate}
  （订单事实 judge 已提到 RefundFlow.run() initial_state，不在 LangGraph 内推理）

decide 节点的三层决策逻辑（硬规则/LLM/校验/重试/P1）已由
tests/services/test_decide_node.py 全覆盖；本文件只测：
1. 图结构（4 节点 + 入口 + 边）
2. 端到端 invoke() 三条路径（synthesize / fetch_policy→synthesize / escalate）

不依赖 MySQL / Qdrant / LLM API，可独立运行。
"""
import json
from unittest.mock import patch

import pytest


# ============ 辅助 ============

def _decide_reply(decision, *, target="ORD001", policy_needed=False,
                  priority="P2", category="复杂场景"):
    """构造 decide 节点期望的 LLM JSON 回复（字符串）。"""
    payload = {
        "decision": decision,
        "confidence": 0.95,
        "target_order_no": target,
        "reason": "测试",
        "escalate": (
            {"enabled": True, "priority": priority, "category": category,
             "handoff_summary": "转人工"}
            if decision == "escalate"
            else {"enabled": False}
        ),
        "need_info": {"enabled": False},
        "reply_key_points": ["测试关键点"],
        "policy_needed": policy_needed,
    }
    return json.dumps(payload, ensure_ascii=False)


def _base_state(query="能退吗", order_no="ORD001"):
    """构造 RefundFlow.run() 已注入好事实的 initial_state。"""
    return {
        "user_id": 42,
        "order_no": order_no,
        "query": query,
        "history": [],
        "orders": [{"order_no": order_no, "status": "delivered", "total_amount": 599.0}],
        "order_info": {"order_no": order_no, "status": "delivered", "total_amount": 599.0},
        "refundable": True,
        "reason": "已签收 3 天，在 7 天无理由退货期限内",
        "status_zh": "已签收",
        "days_since_order": 3,
        "resolver_result": {},
        "decide_retry_count": 0,
        "dialog_turn_count": 1,
        "image_urls": [],
    }


# ============ 图结构 ============

class TestGraphStructure:
    def test_graph_has_4_business_nodes(self):
        """V3 图应有 4 个业务 Node（decide/fetch_policy/synthesize/escalate）"""
        from app.services.refund_graph import refund_graph_app
        expected = {"decide", "fetch_policy", "synthesize", "escalate"}
        actual = set(refund_graph_app.nodes.keys()) - {"__start__"}
        assert actual == expected

    def test_graph_compiles(self):
        """图能正常 compile（单例已 build 过）"""
        from app.services.refund_graph import refund_graph_app
        assert refund_graph_app is not None


# ============ 端到端路径（mock LLM）============

class TestRefundGraphPaths:
    """StateGraph 的 3 条路径分支（decide → ...）"""

    @patch("app.services.refund_graph.get_llm_provider")
    def test_path_synthesize_no_policy(self, mock_provider):
        """decision=synthesize + policy_needed=False → decide → synthesize → END"""
        # 第 1 次 chat = decide 决策；第 2 次 chat = synthesize 生成答案
        mock_provider.return_value.chat.side_effect = [
            {"reply": _decide_reply("synthesize", policy_needed=False)},
            {"reply": "您的订单符合 7 天无理由退货条件，可以退。"},
        ]

        from app.services.refund_graph import refund_graph_app
        result = refund_graph_app.invoke(_base_state())

        assert "可以退" in result["final_answer"]
        # 走 synthesize 路径不查政策
        assert "policy_docs" not in result or result.get("policy_docs") == []

    @patch("app.services.refund_graph.PolicyService")
    @patch("app.services.refund_graph.get_llm_provider")
    def test_path_synthesize_with_policy(self, mock_provider, mock_policy):
        """decision=synthesize + policy_needed=True → decide → fetch_policy → synthesize"""
        mock_provider.return_value.chat.side_effect = [
            {"reply": _decide_reply("synthesize", policy_needed=True)},
            {"reply": "根据 7 天无理由退货政策，您可以退。"},
        ]
        mock_policy.search_policy.return_value = [{"text": "七天无理由退货规则..."}]

        from app.services.refund_graph import refund_graph_app
        result = refund_graph_app.invoke(_base_state())

        assert mock_policy.search_policy.called
        assert result["policy_docs"]
        assert "可以退" in result["final_answer"]

    @patch("app.services.refund_graph.get_llm_provider")
    def test_path_escalate(self, mock_provider):
        """decision=escalate → decide → escalate → END（不走 synthesize，产 escalate_result）"""
        mock_provider.return_value.chat.side_effect = [
            {"reply": _decide_reply("escalate", priority="P0", category="投诉")},
        ]

        from app.services.refund_graph import refund_graph_app
        result = refund_graph_app.invoke(_base_state(query="我要投诉"))

        # escalate 节点产 escalate_result，不产 final_answer
        assert "escalate_result" in result
        assert result["escalate_result"]["priority"] == "P0"
        assert result["escalate_result"]["category"] == "投诉"
        # escalate 路径只调 1 次 LLM（decide），不调 synthesize
        assert mock_provider.return_value.chat.call_count == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
