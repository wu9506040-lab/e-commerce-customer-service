"""
V21 M3-3 测试：第五专家 GeneralistAgent（FC 全科兜底）

覆盖：
1. 注册表：general 意图 → GeneralistAgent（五专家合成）
2. dispatch 透传 agent_runner 事件流（meta/token/done 同一契约）
3. 事件契约：与 4 专家一致，chain_with_secondary 可直接消费
"""
import os
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


def test_registry_contains_generalist():
    from app.services.agents.generalist import GeneralistAgent
    from app.services.agents.registry import all_experts, get_expert

    assert isinstance(get_expert("general"), GeneralistAgent)
    # 不与 4 个业务意图抢路由
    for intent in ("policy_query", "product_query", "order_query",
                   "refund_query", "chitchat"):
        assert not isinstance(get_expert(intent), GeneralistAgent)
    assert len(all_experts()) == 5


def test_generalist_dispatch_delegates_to_agent_runner():
    from app.services.agents.base import AgentContext
    from app.services.agents.generalist import GeneralistAgent

    def fake_run_stream_agent(query, user_id=None, history=None):
        assert query == "帮我写段文案"
        assert user_id == 7
        yield ("meta", {"turn": 1, "final": True})
        yield ("token", "好的")
        yield ("done", {"answer": "好的"})

    with patch("app.services.chat.agent_runner.run_stream_agent",
               side_effect=fake_run_stream_agent) as mock_runner:
        ctx = AgentContext(query="帮我写段文案", user_id=7,
                           intent_result={"primary": "policy_query"})
        events = list(GeneralistAgent().dispatch(ctx))

    mock_runner.assert_called_once()
    assert ("token", "好的") in events
    assert events[-1][0] == "done"


def test_generalist_events_compatible_with_chain():
    """事件流可被 coordinator 透传（同一 (event_type, data) 契约）。"""
    from app.core.config import settings
    from app.services.agents.base import AgentContext
    from app.services.agents.coordinator import chain_with_secondary
    from app.services.agents.generalist import GeneralistAgent

    monkey_enabled = getattr(settings, "ENABLE_DUAL_INTENT_DISPATCH", False)
    try:
        # 直接消费 generalist 流（模拟 generalist 作为主专家被 chain 包裹）
        def fake_run_stream_agent(query, user_id=None, history=None):
            yield ("token", "全科回答")
            yield ("done", {"answer": "全科回答"})

        with patch("app.services.chat.agent_runner.run_stream_agent",
                   side_effect=fake_run_stream_agent):
            ctx = AgentContext(query="开放问题", user_id=1,
                               intent_result={"primary": "policy_query"})
            gen = GeneralistAgent().dispatch(ctx)
            events = list(chain_with_secondary(gen, ctx, "policy_query"))
        assert ("token", "全科回答") in events
        assert events[-1][0] == "done"
    finally:
        object.__setattr__(settings, "ENABLE_DUAL_INTENT_DISPATCH", monkey_enabled)
