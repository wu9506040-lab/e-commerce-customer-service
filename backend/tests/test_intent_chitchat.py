"""
V16.8 P0-C 测试：chitchat 意图 + orchestrator 短路

覆盖：
1. INTENT_RULES 规则层命中 chitchat 类（在 product_query 之前抢先）
2. LLM 兜底 prompt 含 chitchat 类别 + 描述
3. orchestrator.run_stream primary=chitchat 时短路：1 个 meta + 1 个 token + 1 个 done
4. chitchat 短路不走 LLM（不调 LLM provider）
5. meta.intent="chitchat" 字面返回（前端可识别）
"""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture(autouse=True)
def _isolate_intent_state_after():
    """与 test_intent_multi.py 同模式：测试后重置状态"""
    from app.services import intent_service
    from app.services.config_loader import reset_config_loader

    saved_enabled = intent_service._MULTI_INTENT_ENABLED
    saved_top_k = intent_service.TOP_K
    yield
    intent_service._MULTI_INTENT_ENABLED = saved_enabled
    intent_service.TOP_K = saved_top_k
    reset_config_loader()


# =============================================================
# 1. 规则层：chitchat 优先匹配（不被 product_query 兜底抢走）
# =============================================================
class TestChitchatRulePriority:
    """规则顺序敏感：chitchat 必须先于 product_query 匹配整段问候"""

    @pytest.mark.parametrize("query", [
        "在吗",
        "在吗？",
        "在不在",
        "在不在？",
        "在么",
        "在么？",
        "你好",
        "你好！",
        "你好呀",
        "你好啊",
        "您好",
        "您好~",
        "hi",
        "hi!",
        "hello",
        "hello!",
        "hey",
    ])
    def test_classify_returns_chitchat_for_greetings(self, query):
        from app.services.intent_service import IntentService
        result = IntentService.classify(query)
        assert result["primary"] == "chitchat", \
            f"query={query!r} 应该命中 chitchat，实际={result['primary']}"
        # V16.8 P1-G: 短消息（≤4字不含意图关键词）走 rule_short 兜底，长消息（如 "在吗呀"）
        # 走原有 rule regex。两种 method 都接受。
        assert result["method"] in ("rule", "rule_short"), \
            f"method 应是 rule 或 rule_short，实际={result['method']}"
        assert result["intents"][0]["confidence"] == 1.0

    def test_greetings_with_substantive_content_now_classify_as_chitchat(self):
        """V16.8 P1-F trade-off：'你好请问 ZP1 价格' 现在归 chitchat

        旧（V16.8 P0-C）期望 product_query（chitchat 规则严格 ^你好[标点]*$ 不匹配）
        新（P1-F）放宽到 ^你好 → 任何"你好..."开头都归 chitchat
        接受 5% 边界误识别 —— chitchat 短路只回 "在的~"，用户会再追问 ZP1
        """
        from app.services.intent_service import IntentService
        result = IntentService.classify("你好请问 ZP1 价格")
        # V16.8 P1-F：^你好.* 优先命中 → chitchat 而不是 product_query
        assert result["primary"] == "chitchat", \
            f"V16.8 P1-F trade-off：^你好.* 应归 chitchat，实际={result['primary']}"

    def test_intent_type_literal_includes_chitchat(self):
        """IntentType Literal 必须含 chitchat（否则 LLM 返 chitchat 会被 _VALID_INTENTS 拒）"""
        from app.schemas.intent import IntentType
        import typing
        args = typing.get_args(IntentType)
        assert "chitchat" in args, f"IntentType 必须含 chitchat，实际={args}"


# =============================================================
# 2. LLM 兜底 prompt：含 chitchat 类
# =============================================================
class TestLLMClassifyPromptContainsChitchat:
    """prompt 4→5 类必须升级，避免 LLM 误分类"""

    def test_single_intent_prompt_mentions_chitchat(self):
        from app.services.intent_service import IntentService
        # 私有方法，用 _llm_classify 实际触发 prompt 构造
        # 用规则层不会命中的 query（"随便问问" 不在 chitchat 类规则中），强制走 LLM 兜底
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return {"reply": '{"intent":"chitchat","confidence":0.92}'}

        with patch.object(
            __import__("app.core.providers.llm", fromlist=["get_llm_provider"]).get_llm_provider(),
            "chat", side_effect=fake_chat,
        ):
            # 强制走 _llm_classify（单意图路径）
            from app.services import intent_service
            intent_service._MULTI_INTENT_ENABLED = False
            IntentService.classify("麻烦看一下这个商品")  # 不命中任何规则

        prompt = captured["prompt"]
        assert "chitchat" in prompt, "单意图 prompt 必须含 chitchat 类"
        # V16.8 P1-G：去 few-shot 示例后，prompt 不再含示例 Q/A，但 5 类说明 + 输出格式必在
        assert "5" in prompt and ("order_query" in prompt or "退款" in prompt or "policy_query" in prompt), \
            "prompt 文案应包含 5 类说明"

    def test_multi_intent_prompt_mentions_chitchat(self):
        from app.services import intent_service
        from app.services.intent_service import IntentService
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return {"reply": '{"intents":[{"intent":"chitchat","confidence":0.92}],"primary":"chitchat"}'}

        intent_service._MULTI_INTENT_ENABLED = True
        with patch.object(
            __import__("app.core.providers.llm", fromlist=["get_llm_provider"]).get_llm_provider(),
            "chat", side_effect=fake_chat,
        ):
            # V16.8 P1-G: query ≥5 字 + 不含意图关键词，避免 rule_short 兜底短路（仍走 LLM）
            IntentService.classify("请问这个产品好用吗")  # 不命中规则，走 LLM

        prompt = captured["prompt"]
        assert "chitchat" in prompt


# =============================================================
# 3. orchestrator 短路：primary=chitchat 不走 LLM / RAG
# =============================================================
class TestOrchestratorChitchatShortCircuit:

    def _collect(self, gen):
        return list(gen)

    def test_chitchat_short_circuit_yields_3_events(self):
        """primary=chitchat → 1 meta + 1 token + 1 done，meta.intent='chitchat'"""
        from app.services.chat.orchestrator import Synthesizer

        with patch.object(
            __import__("app.services.intent_service", fromlist=["IntentService"]).IntentService,
            "classify",
            return_value={
                "intents": [{"intent": "chitchat", "confidence": 1.0}],
                "primary": "chitchat",
                "method": "rule",
                "entities": {},
                "intent": "chitchat",
                "confidence": 1.0,
            },
        ):
            events = self._collect(Synthesizer.run_stream("你好", user_id=0))

        # chitchat 短路必须产出全部 _intent/meta/token/done 4 事件（顺序敏感）
        # _intent 来自 orchestrator line 230 (IntentService.classify 后内部 yield)
        assert [e[0] for e in events] == ["_intent", "meta", "token", "done"], \
            f"chitchat 短路 4 事件缺失，实际={[e[0] for e in events]}"

        # _intent：内部日志事件，primary=chitchat
        assert events[0][1]["primary"] == "chitchat"

        meta = events[1][1]
        assert meta["intent"] == "chitchat", "meta.intent 必须字面 = 'chitchat'（前端可识别）"
        assert meta["chitchat_short_circuit"] is True

        token = events[2][1]
        assert "在的" in token, "token 文案必须含 CHITCHAT_SHORT_REPLY"

        done = events[3][1]
        assert done["chitchat_short_circuit"] is True
        assert "在的" in done["answer"], "默认回答必须在用 CHITCHAT_SHORT_REPLY 文案"

    def test_chitchat_does_not_invoke_llm(self):
        """chitchat 短路严禁调用 LLM（核心目的：省 token + 快响应）"""
        from app.services.chat.orchestrator import Synthesizer

        with patch.object(
            __import__("app.services.intent_service", fromlist=["IntentService"]).IntentService,
            "classify",
            return_value={
                "intents": [{"intent": "chitchat", "confidence": 1.0}],
                "primary": "chitchat",
                "method": "rule",
                "entities": {},
                "intent": "chitchat",
                "confidence": 1.0,
            },
        ):
            # patch get_llm_provider，验证不调
            with patch("app.core.providers.llm.get_llm_provider") as mock_get_llm:
                mock_get_llm.return_value.chat.side_effect = AssertionError(
                    "chitchat 短路不该调 LLM"
                )
                self._collect(Synthesizer.run_stream("在吗", user_id=0))

    def test_chitchat_does_not_invoke_rag(self):
        """chitchat 短路严禁调 RAG（v12_rag_run_stream）"""
        from app.services.chat.orchestrator import Synthesizer

        with patch.object(
            __import__("app.services.intent_service", fromlist=["IntentService"]).IntentService,
            "classify",
            return_value={
                "intents": [{"intent": "chitchat", "confidence": 1.0}],
                "primary": "chitchat",
                "method": "rule",
                "entities": {},
                "intent": "chitchat",
                "confidence": 1.0,
            },
        ):
            with patch("app.services.chat.orchestrator.v12_rag_run_stream") as mock_rag:
                self._collect(Synthesizer.run_stream("hello", user_id=0))
                assert not mock_rag.called, "chitchat 短路不该触发 v12 RAG"


# =============================================================
# 4. 兼容性：非 chitchat 主流程不受影响
# =============================================================
class TestNonChitchatIntentsUnaffected:
    """chitchat 短路不要破坏 order_query / refund_query / product_query / policy_query"""

    def test_order_query_still_dispatched(self):
        """primary=order_query 仍然进 dispatch 路径（不被 chitchat 短路拦截）"""
        from app.services.chat.orchestrator import Synthesizer

        # mock dispatch_gen 返回 sentinel
        sentinel = [("meta", {"intent": "order_query"}), ("done", {"answer": "x"})]

        with patch.object(
            __import__("app.services.intent_service", fromlist=["IntentService"]).IntentService,
            "classify",
            return_value={
                "intents": [{"intent": "order_query", "confidence": 1.0}],
                "primary": "order_query",
                "method": "rule",
                "entities": {},
                "intent": "order_query",
                "confidence": 1.0,
            },
        ):
            with patch.object(
                Synthesizer, "_handle_order", return_value=iter(sentinel),
            ) as mock_h:
                events = list(Synthesizer.run_stream("我的订单到哪了", user_id=1))
        assert mock_h.called, "order_query 必须仍走 _handle_order"
        assert any(e[1].get("intent") == "order_query" for e in events if e[0] == "meta")
