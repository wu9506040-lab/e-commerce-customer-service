"""双意图双专家协作测试（V21 M3-2：coordinator 链式合成）"""
from __future__ import annotations

from app.core.config import settings
from app.services.agents.base import AgentContext
from app.services.agents.coordinator import (
    chain_with_secondary,
    get_secondary_intent,
    get_secondary_intents,
)
from app.services.agents.experts import PolicyAgent, ProductAgent, RefundAgent


def _ctx(primary="order_query", secondary="policy_query"):
    intents = [{"intent": primary, "confidence": 0.9}]
    if secondary:
        intents.append({"intent": secondary, "confidence": 0.6})
    return AgentContext(
        query="ORD1 我要退款 物流单帮我看看", user_id=7,
        intent_result={"primary": primary, "intents": intents},
        channel="web", session_id="s1",
    )


# ---------- get_secondary_intent ----------

def test_secondary_extracted():
    assert get_secondary_intent({"intents": [
        {"intent": "order_query"}, {"intent": "policy_query"},
    ]}, "order_query") == "policy_query"


def test_secondary_none_when_single():
    assert get_secondary_intent({"intents": [{"intent": "order_query"}]}, "order_query") is None


def test_secondary_skips_same_as_primary():
    assert get_secondary_intent({"intents": [
        {"intent": "order_query"}, {"intent": "order_query"},
    ]}, "order_query") is None


# ---------- chain_with_secondary ----------

def _make_primary_gen():
    def gen():
        yield ("meta", {"intent": "order_query"})
        yield ("token", "主回答")
        yield ("done", {"answer": "主回答"})
    return gen()


def _patch_secondary(monkeypatch, events):
    """patch 次专家 dispatch 产出固定事件。"""
    def fake_dispatch(self, ctx):
        yield from events
    monkeypatch.setattr(PolicyAgent, "dispatch", fake_dispatch)
    monkeypatch.setattr(ProductAgent, "dispatch", fake_dispatch)
    monkeypatch.setattr(RefundAgent, "dispatch", fake_dispatch)


def test_chain_appends_secondary_answer(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DUAL_INTENT_DISPATCH", True, raising=False)
    _patch_secondary(monkeypatch, [("meta", {"intent": "policy_query"}), ("token", "补充回答")])

    ctx = _ctx(primary="order_query", secondary="policy_query")
    events = list(chain_with_secondary(_make_primary_gen(), ctx, "order_query"))

    types = [e[0] for e in events]
    assert types.count("done") == 1            # 次流的 done 被过滤
    # 次流前置 handoff_secondary 提示事件
    sec_meta = [e for e in events if e[0] == "meta" and e[1].get("handoff_secondary")]
    assert sec_meta and sec_meta[0][1]["secondary_expert"] == "policy_expert"
    assert ("token", "补充回答") in events
    assert ("token", "主回答") in events


def test_chain_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DUAL_INTENT_DISPATCH", False, raising=False)
    events = list(chain_with_secondary(_make_primary_gen(), _ctx(), "order_query"))
    assert [e[0] for e in events] == ["meta", "token", "done"]


def test_chain_noop_when_no_secondary(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_DUAL_INTENT_DISPATCH", True, raising=False)
    ctx = _ctx(primary="order_query", secondary=None)   # 单意图
    events = list(chain_with_secondary(_make_primary_gen(), ctx, "order_query"))
    assert [e[0] for e in events] == ["meta", "token", "done"]


def test_chain_secondary_failure_does_not_break_primary(monkeypatch):
    """次专家抛异常 → 只吞掉，主回答完整。"""
    monkeypatch.setattr(settings, "ENABLE_DUAL_INTENT_DISPATCH", True, raising=False)

    def _boom(self, ctx):
        raise RuntimeError("secondary exploded")
        yield  # pragma: no cover

    monkeypatch.setattr(PolicyAgent, "dispatch", _boom)
    monkeypatch.setattr(ProductAgent, "dispatch", _boom)
    monkeypatch.setattr(RefundAgent, "dispatch", _boom)

    events = list(chain_with_secondary(_make_primary_gen(), _ctx(), "order_query"))
    assert ("token", "主回答") in events
    assert ("done", {"answer": "主回答"}) in events


# ---------- V21 M3-4：合并批多意图依次答 ----------

def test_get_secondary_intents_keeps_order_and_dedupes():
    """rule_multi 场景：顺序 = 买家消息顺序；去重；跳过 primary。"""
    assert get_secondary_intents({"intents": [
        {"intent": "order_query"}, {"intent": "product_query"},
        {"intent": "policy_query"}, {"intent": "product_query"},
    ]}, "order_query") == ["product_query", "policy_query"]


def test_chain_answers_all_secondaries_in_message_order(monkeypatch):
    """合并批 3 意图：主专家答完，次专家按 intents 顺序依次补答。"""
    monkeypatch.setattr(settings, "ENABLE_DUAL_INTENT_DISPATCH", True, raising=False)

    calls: list[str] = []

    def fake_dispatch(self, ctx):
        calls.append(ctx.intent_result["primary"])
        yield ("token", f"answer-{ctx.intent_result['primary']}")

    from app.services.agents.experts import OrderAgent
    for agent_cls in (PolicyAgent, ProductAgent, RefundAgent, OrderAgent):
        monkeypatch.setattr(agent_cls, "dispatch", fake_dispatch)

    ctx = _ctx(primary="order_query", secondary=None)
    ctx.intent_result["intents"] = [
        {"intent": "order_query", "confidence": 1.0},
        {"intent": "product_query", "confidence": 1.0},
        {"intent": "policy_query", "confidence": 1.0},
    ]
    events = list(chain_with_secondary(_make_primary_gen(), ctx, "order_query"))

    # 次意图按消息顺序依次答（product 先于 policy）
    assert calls == ["product_query", "policy_query"]
    handoffs = [e[1]["intent"] for e in events
                if e[0] == "meta" and e[1].get("handoff_secondary")]
    assert handoffs == ["product_query", "policy_query"]
    assert ("token", "answer-product_query") in events
    assert ("token", "answer-policy_query") in events
    assert ("token", "主回答") in events
