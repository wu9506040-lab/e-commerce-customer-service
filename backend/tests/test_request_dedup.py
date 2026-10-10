"""V13（2.2，2026-10-11）：请求级去重三刀的回归测试

1. embedding 请求内 memo：同文本一次真实调用，后续复用（begin_embed_memo 作用域）
2. run_stream 复用 chat 预分类：pre_intent 存在且 query 未改写 → 0 次 classify
3. search_policy skip_rerank：展示型检索不调 rerank、截到 top_k
"""
import os

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x")

from unittest.mock import MagicMock, patch

from app.core.embedding import begin_embed_memo, embed_text
from app.core.config import settings
from app.services.chat.orchestrator import Synthesizer
from app.services.policy_service import PolicyService


def _policy_intent(q_primary="policy_query"):
    return {
        "intents": [{"intent": q_primary, "confidence": 1.0}],
        "primary": q_primary,
        "intent": q_primary,
        "method": "rule",
        "confidence": 1.0,
        "entities": {"order_no": None, "sku": None, "keywords": []},
    }


class TestEmbedMemo:

    def test_same_text_embedded_once_in_scope(self):
        calls = []

        def fake_uncached(text):
            calls.append(text)
            return [0.1] * 1024

        token = begin_embed_memo()
        try:
            with patch("app.core.embedding._embed_text_uncached", side_effect=fake_uncached):
                v1 = embed_text("退货运费谁出")
                v2 = embed_text("退货运费谁出")
        finally:
            import app.core.embedding as emb
            emb._embed_memo.reset(token)

        assert len(calls) == 1
        assert v1 == v2

    def test_no_memo_outside_scope(self):
        calls = []

        def fake_uncached(text):
            calls.append(text)
            return [0.2] * 1024

        with patch("app.core.embedding._embed_text_uncached", side_effect=fake_uncached):
            embed_text("q")
            embed_text("q")
        assert len(calls) == 2  # 未 begin 的请求（离线脚本/其他服务）行为不变


class TestPreIntentReuse:

    def test_pre_intent_skips_second_classify(self):
        with patch("app.services.intent_service.IntentService.classify") as mc, \
             patch.object(Synthesizer, "_handle_policy",
                          staticmethod(lambda *a, **k: iter([("done", {"answer": ""})]))):
            list(Synthesizer.run_stream(
                "退货政策是啥", user_id=1, history=None,
                pre_intent=_policy_intent(),
            ))
        mc.assert_not_called()

    def test_rewritten_query_reclassifies(self):
        with patch("app.services.chat.orchestrator.rewrite_query",
                   return_value=("用户问的是订单 ORD20260628004 的退货政策", True)), \
             patch("app.services.intent_service.IntentService.classify",
                   return_value=_policy_intent()) as mc, \
             patch.object(Synthesizer, "_handle_policy",
                          staticmethod(lambda *a, **k: iter([("done", {"answer": ""})]))):
            list(Synthesizer.run_stream(
                "那能退吗", user_id=1, history=[{"role": "user", "content": "我买了个杯子"}],
                pre_intent=_policy_intent(),
            ))
        mc.assert_called_once()  # 改写过必须重分类（pre_intent 基于旧文本已失效）

    def test_no_pre_intent_keeps_legacy_behavior(self):
        with patch("app.services.intent_service.IntentService.classify",
                   return_value=_policy_intent()) as mc, \
             patch.object(Synthesizer, "_handle_policy",
                          staticmethod(lambda *a, **k: iter([("done", {"answer": ""})]))):
            list(Synthesizer.run_stream("退货政策", user_id=1))
        mc.assert_called_once()


class TestSkipRerank:

    def test_skip_rerank_truncates_without_llm(self):
        hits = [
            {"id": f"p{i}", "score": 0.9 - i * 0.01, "payload": {"text": f"d{i}", "source": f"s{i}"}}
            for i in range(15)
        ]
        with patch.object(PolicyService, "_coarse_retrieval", return_value=hits), \
             patch.object(settings, "USE_RERANK", True), \
             patch("app.core.providers.rerank.get_rerank_provider") as mrp:
            out = PolicyService.search_policy("退货", 5, skip_rerank=True)
        mrp.assert_not_called()
        assert len(out) == 5

    def test_normal_path_still_reranks(self):
        hits = [
            {"id": f"p{i}", "score": 0.9 - i * 0.01, "payload": {"text": f"d{i}", "source": f"s{i}"}}
            for i in range(15)
        ]
        reranked = hits[:5]
        with patch.object(PolicyService, "_coarse_retrieval", return_value=hits), \
             patch.object(settings, "USE_RERANK", True), \
             patch("app.core.providers.rerank.get_rerank_provider") as mrp:
            mrp.return_value.rerank.return_value = reranked
            out = PolicyService.search_policy("退货", 5)
        assert mrp.return_value.rerank.called
        assert len(out) == 5
