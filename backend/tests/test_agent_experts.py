"""多 Agent 专家层测试（V21 M3-1：AgentProtocol / 注册表 / 转交）"""
from __future__ import annotations

from app.services.agents.base import AgentContext
from app.services.agents.experts import (
    OrderAgent,
    PolicyAgent,
    ProductAgent,
    RefundAgent,
)
from app.services.agents.registry import get_expert


def test_registry_maps_all_intents():
    assert isinstance(get_expert("policy_query"), PolicyAgent)
    assert isinstance(get_expert("product_query"), ProductAgent)
    assert isinstance(get_expert("order_query"), OrderAgent)
    assert isinstance(get_expert("refund_query"), RefundAgent)
    assert get_expert("chitchat") is None          # 未注册意图回落策略


def test_expert_handles_own_intent():
    a = RefundAgent()
    assert a.handles("refund_query")
    assert not a.handles("order_query")


def test_policy_agent_dispatches_to_synthesizer(monkeypatch):
    """行为等价：PolicyAgent.dispatch 调用原 Synthesizer._handle_policy。"""
    captured = {}

    def _fake_policy(query, intent_result, history, **kw):
        captured["query"] = query
        captured["kw"] = kw
        yield ("token", "ok")

    monkeypatch.setattr(
        "app.services.chat.orchestrator.Synthesizer._handle_policy", staticmethod(_fake_policy),
    )
    ctx = AgentContext(
        query="七天无理由什么条件", user_id=1,
        intent_result={"primary": "policy_query"},
        context_block="cb", search_queries=["q1"],
        secondary_intent_block="sec",
    )
    agent = PolicyAgent()
    events = list(agent.dispatch(ctx))
    assert ("token", "ok") in events
    assert captured["query"] == "七天无理由什么条件"
    assert captured["kw"]["context_block"] == "cb"
    assert captured["kw"]["secondary_intent_block"] == "sec"


def test_refund_agent_xianyu_branch(monkeypatch):
    """闲鱼渠道走 handle_xianyu_refund（原 P0-A 分支归位到专家内）。"""
    captured = {}

    def _fake_xy(*, query, buyer_unb, intent_result):
        captured["buyer_unb"] = buyer_unb
        yield ("token", "xy")

    monkeypatch.setattr(
        "app.services.xianyu.orchestrator_bridge.handle_xianyu_refund", _fake_xy,
    )
    ctx = AgentContext(
        query="我要退款", user_id=100,
        intent_result={"primary": "refund_query"},
        channel="xianyu", buyer_unb="u888",
    )
    events = list(RefundAgent().dispatch(ctx))
    assert captured["buyer_unb"] == "u888"
    assert ("token", "xy") in events


def test_refund_agent_v3_branch(monkeypatch):
    """非闲鱼 + USE_LANGGRAPH_REFUND=True → handle_refund_v3（session 透传）。"""
    from app.core.config import settings

    captured = {}
    monkeypatch.setattr(settings, "USE_LANGGRAPH_REFUND", True, raising=False)

    def _fake_v3(query, user_id, intent_result, **kw):
        captured["session_id"] = kw.get("session_id")
        yield ("token", "v3")

    monkeypatch.setattr(
        "app.services.chat.refund_handler.handle_refund_v3", _fake_v3,
    )
    # create_business_flow 返回 None（未启用 Flow）→ 落 V3
    monkeypatch.setattr(RefundAgent, "_create_flow", staticmethod(lambda ctx: None))

    ctx = AgentContext(
        query="退款", user_id=5, intent_result={"primary": "refund_query"},
        channel="web", session_id="sess-9",
    )
    events = list(RefundAgent().dispatch(ctx))
    assert captured["session_id"] == "sess-9"
    assert ("token", "v3") in events


def test_handoff_redirect_in_ctx():
    """转交契约：refine 可写 redirect_intent（orchestrator 侧消费，最多一次）。"""
    class RedirectAgent(PolicyAgent):
        def refine(self, ctx: AgentContext) -> None:
            ctx.redirect_intent = "refund_query"

    ctx = AgentContext(query="x", user_id=1, intent_result={"primary": "policy_query"})
    RedirectAgent().refine(ctx)
    assert ctx.redirect_intent == "refund_query"
    # 注册表能解析转交目标
    assert isinstance(get_expert(ctx.redirect_intent), RefundAgent)
