"""
Synthesizer 退款路径集成测试（Mock 版）— Sprint 3 更新到 chat.refund_handler 命名空间

策略：mock 外部依赖（OrderTool / PolicyService / qwen_chat），
       验证 synthesizer.run_stream 在 refund_query 意图下的：
       - 默认走 V2.x
       - USE_LANGGRAPH_REFUND=true 走 V3 LangGraph 版
       - V3 异常时 fallback 到 V2
       - V3 SSE 协议正确（meta / token）

Sprint 3 拆分后：
- _handle_refund_v3 / _handle_refund_v2 从 Synthesizer class 移到 chat.refund_handler 模块级函数
- tests patches 改为 chat.refund_handler / chat.stream_dispatcher / chat.orchestrator 三个 namespace

不依赖 MySQL / Qdrant / LLM API，可独立运行。
"""
from unittest.mock import patch, MagicMock

import pytest


# ============ 辅助：构造测试数据 ============

def make_order(status="delivered", days_ago=3, order_no="ORD001"):
    """生成 mock 订单"""
    import datetime
    create_time = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
    return {
        "order_no": order_no,
        "status": status,
        "total_amount": 599.0,
        "create_time": create_time,
    }


def make_intent_result(intent="refund_query", order_no=None):
    """构造 IntentService.classify 的返回"""
    return {
        "intent": intent,
        "confidence": 1.0,
        "method": "rule",
        "entities": {
            "order_no": order_no,
            "sku": None,
            "keywords": [],
        },
    }


def collect_events(generator):
    """把 generator 展开成 list[event]"""
    events = []
    for ev in generator:
        events.append(ev)
    return events


# ============ 测试 V2 / V3 dispatch ============

class TestRefundDispatch:
    """测试 run_stream 的 refund_query 分派逻辑"""

    @patch("app.services.chat.orchestrator.v12_rag_run_stream")
    @patch("app.services.refund_graph.get_llm_provider")
    @patch("app.services.refund_graph.PolicyService")
    @patch("app.services.refund_graph.OrderTool")
    @patch("app.services.chat.refund_handler.OrderService")
    def test_default_uses_v2(self, mock_order_svc, mock_tool, mock_policy, mock_provider, mock_v12):
        """默认 USE_LANGGRAPH_REFUND=False → 走 V2"""
        # V2 用的 service
        mock_order_svc.list_user_orders.return_value = [make_order()]

        # V2 不直接调 LangGraph 的 tool/policy/qwen，但 V1.2 fallback 可能调 v12
        mock_v12.return_value = iter([])

        from app.services.chat.orchestrator import Synthesizer
        events = collect_events(Synthesizer.run_stream(
            query="能退吗",
            user_id=42,
            history=None,
        ))

        # 默认情况下 refund_query 不会调 refund_graph_app
        # 通过 mock OrderService.list_user_orders 是否被调用判断（V2 兜底 order_no 用它）
        assert mock_order_svc.list_user_orders.called

    @patch("app.services.chat.refund_handler.OrderService")
    @patch("app.services.chat.orchestrator.settings")
    def test_v3_when_env_true(self, mock_settings, mock_order_svc):
        """USE_LANGGRAPH_REFUND=True → 走 V3 LangGraph 版"""
        mock_settings.USE_LANGGRAPH_REFUND = True
        mock_settings.LOG_LEVEL = "INFO"

        # V3 走 LangGraph，不走 V2 的 OrderService.list_user_orders 兜底
        # （除非 order_no 为空时还会用一次）
        mock_order_svc.list_user_orders.return_value = [make_order()]

        # 但 LangGraph 内部的 fetch_order 走的是 OrderTool（不同的 import path）
        with patch("app.services.refund_graph.OrderTool") as mock_tool, \
             patch("app.services.refund_graph.PolicyService") as mock_policy, \
             patch("app.services.refund_graph.get_llm_provider") as mock_provider:
            mock_tool.get_order_by_no.return_value = make_order(days_ago=3)
            mock_policy.search_policy.return_value = [{"text": "7天无理由"}]
            mock_provider.return_value.chat.return_value = {"reply": "可以退"}

            from app.services.chat.orchestrator import Synthesizer
            events = collect_events(Synthesizer.run_stream(
                query="三天前买的能退吗",
                user_id=42,
                order_no="ORD001",  # 必传：M9.5 修复后无 order_no 直接 early-return 不会进 LangGraph
                history=None,
            ))

            # V3 路径：应该调 LangGraph 内部的 OrderTool（不是 refund_handler.OrderService）
            assert mock_tool.get_order_by_no.called
            assert mock_provider.return_value.chat.called


# ============ 测试 handle_refund_v3 4 条路径 ============

class TestHandleRefundV3:
    """测试 chat.refund_handler.handle_refund_v3 的 4 条路径"""

    @patch("app.services.chat.refund_handler.OrderService")
    def test_unauthenticated_user(self, mock_order_svc):
        """未登录用户：返回「请登录」模板"""
        from app.services.chat.refund_handler import handle_refund_v3
        events = collect_events(handle_refund_v3(
            query="能退吗",
            user_id=0,  # ANONYMOUS_USER_ID
            intent_result=make_intent_result(),
        ))

        # 应该有 meta + token（含"登录"字样）
        assert any(ev[0] == "meta" for ev in events)
        assert any(ev[0] == "token" and "登录" in ev[1] for ev in events)
        assert not mock_order_svc.called  # 不查订单

    @patch("app.services.chat.refund_handler.OrderService")
    def test_no_orders(self, mock_order_svc):
        """没订单：返回「请提供订单号」模板"""
        mock_order_svc.list_user_orders.return_value = []

        from app.services.chat.refund_handler import handle_refund_v3
        events = collect_events(handle_refund_v3(
            query="能退吗",
            user_id=42,
            intent_result=make_intent_result(),
        ))

        assert any(ev[0] == "meta" for ev in events)
        assert any(ev[0] == "token" and "订单号" in ev[1] for ev in events)

    @patch("app.services.refund_graph.get_llm_provider")
    @patch("app.services.refund_graph.OrderTool")
    @patch("app.services.chat.refund_handler.OrderService")
    def test_path1_refundable_with_policy(self, mock_order_svc, mock_tool, mock_provider):
        """路径 1：可退 + 查政策 → synthesize"""
        mock_order_svc.list_user_orders.return_value = [make_order()]  # 兜底 order_no
        mock_tool.get_order_by_no.return_value = make_order("delivered", days_ago=3)
        mock_provider.return_value.chat.return_value = {"reply": "符合 7 天无理由"}

        from app.services.chat.refund_handler import handle_refund_v3
        events = collect_events(handle_refund_v3(
            query="三天前买的能退吗",
            user_id=42,
            intent_result=make_intent_result(),
            order_no="ORD001",  # 必传：M9.5 防串单修复后无 order_no 不会进 LangGraph
        ))

        # 验证 SSE 协议
        meta_events = [ev for ev in events if ev[0] == "meta"]
        token_events = [ev for ev in events if ev[0] == "token"]

        assert len(meta_events) >= 1, "至少应有一个 meta 事件"
        assert len(token_events) >= 1, "至少应有一个 token 事件"

        # meta 应包含 v3_engine=langgraph
        meta = meta_events[0][1]
        assert meta.get("v3_engine") == "langgraph"

        # meta 应包含 judge 的判断结果
        assert "refundable" in meta
        assert meta["refundable"] is True
        # delivered 订单按签收日算：create_time + 2 天偏移，days_ago=3 → 实际 1 天
        assert meta["days_since_order"] == 1

        # token 应包含 LLM 最终答案
        assert any("符合" in ev[1] for ev in token_events)

    @patch("app.services.refund_graph.OrderTool")
    @patch("app.services.chat.refund_handler.OrderService")
    def test_path2_quality_issue_escalate(self, mock_order_svc, mock_tool):
        """路径 2：质量问题无凭证 → escalate"""
        mock_order_svc.list_user_orders.return_value = [make_order()]
        mock_tool.get_order_by_no.return_value = make_order("delivered", days_ago=3)

        from app.services.chat.refund_handler import handle_refund_v3
        events = collect_events(handle_refund_v3(
            query="质量有问题能退吗",
            user_id=42,
            intent_result=make_intent_result(),
            order_no="ORD001",  # 必传：防串单 guard 要求有 order_no 才进 LangGraph
        ))

        # 应该走 escalate 路径
        token_events = [ev for ev in events if ev[0] == "token"]
        assert len(token_events) >= 1
        assert any("人工" in ev[1] for ev in token_events)

    @patch("app.services.refund_graph.get_llm_provider")
    @patch("app.services.refund_graph.OrderTool")
    @patch("app.services.chat.refund_handler.OrderService")
    def test_path3_over_7_days(self, mock_order_svc, mock_tool, mock_provider):
        """路径 3：超过 7 天 → 不可退 → synthesize"""
        mock_order_svc.list_user_orders.return_value = [make_order(days_ago=15)]
        mock_tool.get_order_by_no.return_value = make_order("delivered", days_ago=15)
        mock_provider.return_value.chat.return_value = {"reply": "已超过 7 天，不符合退款条件"}

        from app.services.chat.refund_handler import handle_refund_v3
        events = collect_events(handle_refund_v3(
            query="能退吗",
            user_id=42,
            intent_result=make_intent_result(order_no="ORD002"),
        ))

        meta_events = [ev for ev in events if ev[0] == "meta"]
        token_events = [ev for ev in events if ev[0] == "token"]

        # meta 应该显示 refundable=False
        meta = meta_events[0][1]
        assert meta.get("refundable") is False

        # token 包含 LLM 答案
        assert any("超过" in ev[1] or "不符合" in ev[1] for ev in token_events)


# ============ 测试 fallback 机制 ============

class TestFallback:
    """LangGraph 异常时 fallback 到 V2"""

    @patch("app.services.refund_graph.OrderTool")
    @patch("app.services.chat.refund_handler.OrderService")
    @patch("app.services.chat.refund_handler.RefundService")
    @patch("app.services.chat.stream_dispatcher.get_llm_provider")
    def test_langgraph_failure_fallback_to_v2(
        self, mock_provider, mock_refund_svc, mock_order_svc, mock_tool,
    ):
        """LangGraph 内部抛异常 → fallback 到 V2"""
        # V2 链路需要的 mock
        mock_order_svc.list_user_orders.return_value = [make_order()]
        mock_refund_svc.check_refundable_with_policy.return_value = {
            "tool_result": {"refundable": True, "reason": "7 天无理由", "order_status": "delivered"},
            "policy_docs": [{"text": "七天无理由..."}],
            "synthesizable": True,
        }
        # V2 最终调 stream_chat 流式输出
        mock_provider.return_value.stream_chat.return_value = iter(["可以", "退"])

        # LangGraph 内部抛异常
        mock_tool.get_order_by_no.side_effect = RuntimeError("DB down")

        from app.services.chat.refund_handler import handle_refund_v3
        events = collect_events(handle_refund_v3(
            query="能退吗",
            user_id=42,
            intent_result=make_intent_result(),
            order_no="ORD001",  # 必传：否则 M9.5 guard 直接返回，不进 LangGraph，也触发不了 fallback
        ))

        # fallback 应该触发 V2 → 调 RefundService.check_refundable_with_policy
        assert mock_refund_svc.check_refundable_with_policy.called
        # 应该至少有一个 token 事件
        assert any(ev[0] == "token" for ev in events)


# ============ 测试 SSE 协议格式 ============

class TestSSEProtocol:
    """验证 SSE 事件格式（meta / token 的 payload 结构）"""

    @patch("app.services.refund_graph.get_llm_provider")
    @patch("app.services.refund_graph.PolicyService")
    @patch("app.services.refund_graph.OrderTool")
    @patch("app.services.chat.refund_handler.OrderService")
    def test_meta_payload_structure(self, mock_order_svc, mock_tool, mock_policy, mock_provider):
        """meta 事件 payload 应包含 V3 特有字段"""
        mock_order_svc.list_user_orders.return_value = [make_order()]
        mock_tool.get_order_by_no.return_value = make_order(days_ago=3)
        mock_policy.search_policy.return_value = [{"text": "..."}]
        mock_provider.return_value.chat.return_value = {"reply": "OK"}

        from app.services.chat.refund_handler import handle_refund_v3
        events = collect_events(handle_refund_v3(
            query="能退吗",
            user_id=42,
            intent_result=make_intent_result(),
            order_no="ORD001",  # 必传：否则 guard early-return，LangGraph 不跑，meta 没 v3_engine
        ))

        meta_events = [ev for ev in events if ev[0] == "meta"]
        assert len(meta_events) == 1

        meta = meta_events[0][1]
        # 必含字段
        assert meta["intent"] == "refund_query"
        assert meta["v3_engine"] == "langgraph"
        assert "refundable" in meta
        assert "reason" in meta
        assert "days_since_order" in meta
        assert "entities" in meta
        # 上下文/分数（V3 不从 RAG 拿，保持空列表兼容 V2 协议）
        assert meta["contexts"] == []
        assert meta["scores"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
