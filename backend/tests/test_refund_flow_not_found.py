"""
V13（2026-10-10）H1 修复：自报订单号查无 → 确定性短路，不进 LangGraph

根因（审计 2026-10-09 / 修复 2026-10-10）：refund_flow.py 自报单号分支
get_order_by_no 返回 None → order_info={} → judge 跳过、reason 空 →
synthesize 被硬约束#5 逼出臆造话术（V2 check_refundable 有"订单不存在"
分支，V3 重构丢失，属回归）。

断言（与 eval_refund_accuracy H1 用例对齐）：
1. 回复含 [不存在/请检查]，banned 词零触碰
2. meta.refundable is False
3. LangGraph 0 次调用（不调 LLM）
4. 对照：订单存在 → 不短路，照常进图
"""
import os
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")

from app.services.business_flow.refund_flow import RefundFlow


def _intent_result(order_no: str) -> dict:
    """构造 classify() 真实返回结构（V12：intents + primary + entities）"""
    return {
        "intents": [{"intent": "refund_query", "confidence": 1.0}],
        "primary": "refund_query",
        "intent": "refund_query",
        "method": "rule",
        "confidence": 1.0,
        "entities": {"order_no": order_no, "sku": None, "keywords": []},
    }


class TestRefundFlowOrderNotFound:

    def test_unknown_order_short_circuits_without_llm(self):
        """查无订单：确定性话术 + refundable=False + 图不被调用"""
        with patch(
            "app.services.business_flow.refund_flow.OrderTool.get_order_by_no",
            return_value=None,
        ), patch(
            "app.services.business_flow.refund_flow._db_reachable",
            return_value=True,
        ), patch(
            "app.services.business_flow.refund_flow.refund_graph_app"
        ) as mock_graph:
            events = list(RefundFlow(
                query="ORD99999999999 能退吗",
                user_id=42,
                intent_result=_intent_result("ORD99999999999"),
            ).run())

        mock_graph.stream.assert_not_called()

        metas = [e[1] for e in events if e[0] == "meta"]
        assert any(m.get("refundable") is False for m in metas)
        assert any(m.get("flow_stage") == "fetch_order" for m in metas)

        texts = [e[1] for e in events if e[0] == "token"]
        assert texts, "短路必须给确定性文本"
        assert "不存在" in texts[0] and "请检查" in texts[0]
        for banned in ("可以退", "超过 7 天", "已签收", "支持您"):
            assert banned not in texts[0]
        assert any(e[0] == "done" for e in events)

    def test_valid_order_still_goes_to_graph(self):
        """对照：订单存在 → 不短路，照常进 LangGraph"""
        fake_order = {
            "order_no": "ORD20260628004",
            "status": "delivered",
            "total_amount": 599.0,
        }
        with patch(
            "app.services.business_flow.refund_flow.OrderTool.get_order_by_no",
            return_value=fake_order,
        ), patch(
            "app.services.business_flow.refund_flow._judge_basic_refundable",
            return_value=(True, "已签收 3 天，在 7 天无理由退货期限内", 3, "已签收"),
        ), patch(
            "app.services.business_flow.refund_flow.refund_graph_app"
        ) as mock_graph:
            mock_graph.stream.return_value = iter([])
            events = list(RefundFlow(
                query="ORD20260628004 能退吗",
                user_id=42,
                intent_result=_intent_result("ORD20260628004"),
            ).run())

        mock_graph.stream.assert_called_once()
        not_found_metas = [
            e[1] for e in events
            if e[0] == "meta" and e[1].get("reason") == "订单不存在"
        ]
        assert not_found_metas == []
