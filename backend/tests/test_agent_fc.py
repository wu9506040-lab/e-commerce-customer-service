"""
C2 Agent Function Calling 框架测试

覆盖：
1. 灰度门禁：ENABLE_AGENT_FC=False → 抛 RuntimeError
2. 单轮 FC：LLM 直接返最终答案（无 tool_calls）
3. 单工具调用：LLM 第 1 轮返 tool_calls → 第 2 轮综合
4. 多工具调用：LLM 多轮 tool_calls，最终给出答案
5. 超限：LLM 永远返 tool_calls → MAX_AGENT_TURNS 后 fallback
6. Orchestrator 集成：ENABLE_AGENT_FC=True → 走 agent_runner
7. Fallback：agent_runner 异常 → orchestrator 走 V1.2 RAG
"""
import os
import sys
from pathlib import Path
from typing import Any, Tuple
from unittest.mock import patch, MagicMock

import pytest

# pytest 不走 __main__，env 必须在模块顶部 setdefault
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")

# path 处理：tests/ 在 backend/tests/，要能 import app.*
TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))


# =============================================================
# 工具函数
# =============================================================
def _make_fake_tool_call(call_id: str, name: str, arguments: str) -> dict:
    """构造 OpenAI FC 风格的 tool_call dict（agent_runner 抽出的结构）。"""
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def _consume(gen) -> list[Tuple[str, Any]]:
    """消费 generator 返回所有事件（驱动 generator 完整执行）。"""
    events = []
    for event in gen:
        events.append(event)
    return events


def _make_mock_llm(chat_responses: list[dict]):
    """构造 mock LLM provider：依次返 chat_responses 列表里的响应。

    chat_responses[i] 是 dict，可以含 reply / tool_calls 字段。
    """
    mock_llm = MagicMock()
    mock_llm.chat.side_effect = chat_responses
    return mock_llm


# =============================================================
# 1. 灰度门禁
# =============================================================
class TestAgentFCGate:
    """验证 ENABLE_AGENT_FC 灰度开关的边界行为。"""

    def test_disabled_raises_runtime_error(self, monkeypatch):
        """ENABLE_AGENT_FC=False 时必须抛 RuntimeError（让 orchestrator 走 fallback）。"""
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", False)

        with pytest.raises(RuntimeError, match="ENABLE_AGENT_FC"):
            list(run_stream_agent("test query", user_id=1))

    def test_empty_query_raises_value_error(self, monkeypatch):
        """query 为空必须抛 ValueError（input 校验）。"""
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)

        with pytest.raises(ValueError, match="query"):
            list(run_stream_agent("", user_id=1))

    def test_whitespace_query_raises_value_error(self, monkeypatch):
        """query 全空白也必须抛 ValueError。"""
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)

        with pytest.raises(ValueError, match="query"):
            list(run_stream_agent("   ", user_id=1))


# =============================================================
# 2. 单轮 FC（LLM 直接返最终答案）
# =============================================================
class TestAgentFCSingleTurn:
    """LLM 第 1 轮就给出最终答案（不调任何工具）。"""

    def test_direct_answer_yields_meta_tokens_done(self, monkeypatch):
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)
        mock_llm = _make_mock_llm([
            {"reply": "您好，有什么可以帮您？", "tool_calls": None,
             "model": "qwen", "usage": {}, "tool_calls_field": None},
        ])

        with patch("app.services.chat.agent_runner.get_llm_provider",
                   return_value=mock_llm):
            events = _consume(run_stream_agent("hi", user_id=1))

        # 验证 yield 序列
        event_types = [e[0] for e in events]
        assert "meta" in event_types
        assert "token" in event_types
        assert "done" in event_types

        # final meta
        final_metas = [e[1] for e in events if e[0] == "meta" and e[1].get("final")]
        assert len(final_metas) == 1
        assert final_metas[0]["tool_used_count"] == 0

        # token 数 = reply 字符数（伪流式）
        tokens = [e[1] for e in events if e[0] == "token"]
        assert "".join(tokens) == "您好，有什么可以帮您？"

        # done 事件
        done_events = [e[1] for e in events if e[0] == "done"]
        assert done_events[0]["answer"] == "您好，有什么可以帮您？"

        # LLM 只调了 1 次
        assert mock_llm.chat.call_count == 1

    def test_llm_called_with_tools_and_auto_choice(self, monkeypatch):
        """验证 LLM.chat 必须带 tools + tool_choice='auto'。"""
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)
        mock_llm = _make_mock_llm([
            {"reply": "ok", "tool_calls": None, "model": "qwen", "usage": {}},
        ])

        with patch("app.services.chat.agent_runner.get_llm_provider",
                   return_value=mock_llm):
            list(run_stream_agent("q", user_id=1))

        call_kwargs = mock_llm.chat.call_args.kwargs
        assert call_kwargs["tool_choice"] == "auto"
        assert isinstance(call_kwargs["tools"], list)
        assert len(call_kwargs["tools"]) >= 1
        # tools 必须是 OpenAI FC 格式
        assert call_kwargs["tools"][0]["type"] == "function"


# =============================================================
# 3. 单工具调用
# =============================================================
class TestAgentFCSingleToolCall:
    """LLM 第 1 轮调工具，第 2 轮综合。"""

    def test_one_tool_call_then_final(self, monkeypatch):
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)

        # 第 1 轮 LLM 决定调 lookup_order
        tc1 = _make_fake_tool_call("call_001", "lookup_order",
                                   '{"order_no": "ORD001"}')
        # 第 2 轮 LLM 综合给出最终答案
        mock_llm = _make_mock_llm([
            {"reply": None, "tool_calls": [tc1], "model": "qwen", "usage": {}},
            {"reply": "您的订单 ORD001 状态：已发货。", "tool_calls": None,
             "model": "qwen", "usage": {}},
        ])

        # mock registry.dispatch 返 fake result
        fake_result = {"order": {"status": "shipped"}, "items": []}
        with patch("app.services.chat.agent_runner.get_llm_provider",
                   return_value=mock_llm), \
             patch("app.services.chat.agent_runner.dispatch",
                   return_value=fake_result) as mock_dispatch:
            events = _consume(run_stream_agent("ORD001 状态？", user_id=42))

        # yield 序列：meta(tool_call) + meta(tool_result) + meta(final) + tokens + done
        event_types = [e[0] for e in events]
        assert event_types.count("meta") == 3  # tool_call + tool_result + final
        assert "token" in event_types
        assert "done" in event_types

        # tool_call meta
        tool_call_metas = [e[1] for e in events if e[0] == "meta"
                           and "tool_call" in e[1]]
        assert len(tool_call_metas) == 1
        assert tool_call_metas[0]["tool_call"]["name"] == "lookup_order"

        # tool_result meta（结果应被截断/复制）
        tool_result_metas = [e[1] for e in events if e[0] == "meta"
                             and "tool_result" in e[1]]
        assert len(tool_result_metas) == 1
        assert tool_result_metas[0]["tool_result"]["name"] == "lookup_order"

        # final meta 的 tool_used_count = 1
        final_metas = [e[1] for e in events if e[0] == "meta" and e[1].get("final")]
        assert final_metas[0]["tool_used_count"] == 1

        # dispatch 必须以正确参数被调
        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.args[0] == "lookup_order"
        # ctx 应注入 user_id
        ctx = mock_dispatch.call_args.args[2]
        assert ctx.user_id == 42

        # LLM 调了 2 次
        assert mock_llm.chat.call_count == 2

    def test_tool_result_appended_to_messages(self, monkeypatch):
        """第 2 轮 LLM.chat 的 messages 必须含 tool 消息（OpenAI FC 必需）。"""
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)
        tc1 = _make_fake_tool_call("call_xyz", "search_policy", '{"query": "运费"}')
        mock_llm = _make_mock_llm([
            {"reply": None, "tool_calls": [tc1], "model": "qwen", "usage": {}},
            {"reply": "包邮政策...", "tool_calls": None, "model": "qwen", "usage": {}},
        ])

        with patch("app.services.chat.agent_runner.get_llm_provider",
                   return_value=mock_llm), \
             patch("app.services.chat.agent_runner.dispatch",
                   return_value={"policy_docs": [{"text": "..."}]}):
            list(run_stream_agent("运费多少", user_id=1))

        # 第 2 次 LLM.chat 的 messages 必须含 tool role + tool_call_id
        second_call_msgs = mock_llm.chat.call_args_list[1].kwargs["messages"]
        tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_xyz"
        assert "policy_docs" in tool_msgs[0]["content"]


# =============================================================
# 4. 多轮 FC（连续多个 tool_calls）
# =============================================================
class TestAgentFCMultiTurn:
    """LLM 多轮返 tool_calls，最终给出答案。"""

    def test_two_tool_calls_in_two_turns(self, monkeypatch):
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)

        tc1 = _make_fake_tool_call("c1", "lookup_order", '{"order_no": "O1"}')
        tc2 = _make_fake_tool_call("c2", "search_product", '{"keyword": "耳机"}')
        mock_llm = _make_mock_llm([
            {"reply": None, "tool_calls": [tc1], "model": "qwen", "usage": {}},
            {"reply": None, "tool_calls": [tc2], "model": "qwen", "usage": {}},
            {"reply": "您的订单耳机颜色是黑色", "tool_calls": None,
             "model": "qwen", "usage": {}},
        ])

        with patch("app.services.chat.agent_runner.get_llm_provider",
                   return_value=mock_llm), \
             patch("app.services.chat.agent_runner.dispatch",
                   side_effect=[
                       {"order": {"sku": "SKU001"}},
                       {"products": [{"name": "耳机", "color": "黑"}]},
                   ]):
            events = _consume(run_stream_agent("订单耳机颜色", user_id=1))

        # LLM 调了 3 次
        assert mock_llm.chat.call_count == 3
        # final meta tool_used_count = 2
        final_metas = [e[1] for e in events if e[0] == "meta" and e[1].get("final")]
        assert final_metas[0]["tool_used_count"] == 2
        assert final_metas[0]["turn"] == 3


# =============================================================
# 5. 超限（MAX_AGENT_TURNS 后 fallback）
# =============================================================
class TestAgentFCMaxTurns:
    """LLM 永远返 tool_calls → 达到 MAX_AGENT_TURNS 后返 fallback。"""

    def test_max_turns_reached_yields_fallback(self, monkeypatch):
        from app.core.config import settings
        from app.services.chat.agent_runner import run_stream_agent

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)
        monkeypatch.setattr(settings, "MAX_AGENT_TURNS", 3)

        # 永远返 tool_calls（LLM 死循环模拟）
        tc = _make_fake_tool_call("c1", "lookup_order", '{"order_no": "O"}')
        mock_llm = _make_mock_llm([
            {"reply": None, "tool_calls": [tc], "model": "qwen", "usage": {}}
        ] * 5)  # 准备 5 次响应，实际只用 3 次

        with patch("app.services.chat.agent_runner.get_llm_provider",
                   return_value=mock_llm), \
             patch("app.services.chat.agent_runner.dispatch",
                   return_value={"order": {"status": "shipped"}}):
            events = _consume(run_stream_agent("死循环测试", user_id=1))

        # LLM 只调 MAX_AGENT_TURNS 次
        assert mock_llm.chat.call_count == 3

        # yield 序列含 max_turns_reached meta
        max_metas = [e[1] for e in events if e[0] == "meta"
                     and e[1].get("max_turns_reached")]
        assert len(max_metas) == 1

        # 最终 done 的 answer 是 fallback 文案
        done_events = [e[1] for e in events if e[0] == "done"]
        assert "复杂度限制" in done_events[0]["answer"]


# =============================================================
# 6. Orchestrator 集成（FC 灰度门禁）
# =============================================================
class TestOrchestratorFCIntegration:
    """orchestrator 在 ENABLE_AGENT_FC=True 时必须走 agent_runner。"""

    def test_enabled_routes_to_agent_runner(self, monkeypatch):
        from app.core.config import settings
        from app.services.chat.orchestrator import Synthesizer

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)

        # mock agent_runner.run_stream_agent
        def fake_run_stream_agent(query, user_id=None, history=None):
            yield ("meta", {"agent_marker": True})
            yield ("token", "X")
            yield ("done", {"answer": "from agent"})

        with patch("app.services.chat.agent_runner.run_stream_agent",
                   side_effect=fake_run_stream_agent) as mock_runner:
            events = _consume(Synthesizer.run_stream("test", user_id=1))

        # 确认 orchestrator 调了 agent_runner
        mock_runner.assert_called_once()
        assert mock_runner.call_args.kwargs["user_id"] == 1

        # 确认 events 来源于 agent_runner（不含 intent classify 产生的 meta）
        agent_metas = [e[1] for e in events if e[0] == "meta"
                       and e[1].get("agent_marker")]
        assert len(agent_metas) == 1

    def test_disabled_does_not_route_to_agent_runner(self, monkeypatch):
        """ENABLE_AGENT_FC=False 时 orchestrator 调 agent_runner.run_stream_agent 0 次。"""
        from app.core.config import settings
        from app.services.chat.orchestrator import Synthesizer

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", False)

        with patch("app.services.chat.agent_runner.run_stream_agent") as mock_runner, \
             patch("app.services.chat.orchestrator.v12_rag_run_stream") as mock_v12:
            # 让 v12_rag_run_stream 立即返一个 done 事件，避免真跑 RAG
            mock_v12.return_value = iter([("done", {"answer": "v12 fallback"})])
            # mock intent_service 防止真跑分类
            with patch("app.services.intent_service.IntentService.classify",
                       return_value={"intent": "policy_query", "entities": {},
                                     "method": "rule", "confidence": 1.0}):
                events = _consume(Synthesizer.run_stream("test", user_id=1))

        # agent_runner 0 次调用（走的是普通 intent 分派路径）
        mock_runner.assert_not_called()
        # events 应来自普通路径（不包含 agent_marker）
        agent_metas = [e[1] for e in events if e[0] == "meta"
                       and e[1].get("agent_marker")]
        assert len(agent_metas) == 0


# =============================================================
# 7. Orchestrator FC 异常 fallback 到 V1.2
# =============================================================
class TestOrchestratorFCFallback:
    """agent_runner 异常时 orchestrator 必须 fallback 到 V1.2 RAG。"""

    def test_agent_runner_exception_falls_back_to_v12(self, monkeypatch):
        from app.core.config import settings
        from app.services.chat.orchestrator import Synthesizer

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)

        # agent_runner 抛异常（mock LLM 失败）
        def broken_run_stream_agent(query, user_id=None, history=None):
            yield ("meta", {"turn": 1})
            raise RuntimeError("mock LLM failure")
            yield  # noqa: 让 Python 识别 generator

        with patch("app.services.chat.agent_runner.run_stream_agent",
                   side_effect=broken_run_stream_agent) as mock_runner, \
             patch("app.services.chat.orchestrator.v12_rag_run_stream") as mock_v12:
            mock_v12.return_value = iter([("done", {"answer": "fallback ok"})])
            events = _consume(Synthesizer.run_stream("test", user_id=1))

        # agent_runner 被调（异常被抛出）
        mock_runner.assert_called_once()
        # v12 fallback 必须被调
        mock_v12.assert_called_once()
        # 最终 answer 应来自 fallback
        done_events = [e[1] for e in events if e[0] == "done"]
        assert done_events[0]["answer"] == "fallback ok"

    def test_agent_runner_disabled_runtime_error_falls_back(self, monkeypatch):
        """ENABLE_AGENT_FC=False 时 agent_runner 抛 RuntimeError → orchestrator fallback。"""
        from app.core.config import settings
        from app.services.chat.orchestrator import Synthesizer

        monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)

        # agent_runner 内部检测到 ENABLE_AGENT_FC=False 抛 RuntimeError
        # （monkeypatch 的 ENABLE_AGENT_FC=False 仅对 run_stream_agent 内部生效，
        #   orchestrator 自己读的是 settings，模拟"配置不一致"）
        def broken_run_stream_agent(query, user_id=None, history=None):
            raise RuntimeError("ENABLE_AGENT_FC=False")
            yield  # noqa

        with patch("app.services.chat.agent_runner.run_stream_agent",
                   side_effect=broken_run_stream_agent), \
             patch("app.services.chat.orchestrator.v12_rag_run_stream") as mock_v12:
            mock_v12.return_value = iter([("done", {"answer": "fallback ok"})])
            events = _consume(Synthesizer.run_stream("test", user_id=1))

        mock_v12.assert_called_once()
        done_events = [e[1] for e in events if e[0] == "done"]
        assert done_events[0]["answer"] == "fallback ok"