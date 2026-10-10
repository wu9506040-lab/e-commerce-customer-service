"""V13（1.3，2026-10-10）：分派 handler 中途异常 → 禁止"已发 token 后整流重答"

根因（审计发现）：dispatch 路径抛异常时无条件 fallback V1.2 从头再跑——
若此前已下发过 token，chat.py 累加出"半截 + 重答全文"的拼接答案落库，
客户端也收到第二个 meta 与双份内容。
修复：消费循环跟踪 yielded_token；已发 token → 只追加固定收尾话术 + done；
未发 token → 才允许 V1.2 整流 fallback（原行为保留）。
"""
import os

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")

from unittest.mock import patch

from app.services.chat.orchestrator import Synthesizer


def _policy_intent():
    return {
        "intents": [{"intent": "policy_query", "confidence": 1.0}],
        "primary": "policy_query",
        "intent": "policy_query",
        "method": "rule",
        "confidence": 1.0,
        "entities": {"order_no": None, "sku": None, "keywords": []},
    }


class TestFallbackPartialGuard:

    def test_no_reflow_after_partial_tokens(self):
        """已发 1 个 token 后抛异常 → 不整流重答，固定话术收尾，v12 不被调用"""
        def _broken_policy(*a, **k):
            yield ("meta", {"intent": "policy_query", "contexts": [], "scores": []})
            yield ("token", "半截答")
            raise RuntimeError("handler 中途炸")

        with patch("app.services.intent_service.IntentService.classify",
                   return_value=_policy_intent()), \
             patch.object(Synthesizer, "_handle_policy", staticmethod(_broken_policy)), \
             patch("app.services.chat.orchestrator.v12_rag_run_stream") as mock_v12:
            events = list(Synthesizer.run_stream("怎么退货", user_id=1))

        mock_v12.assert_not_called()
        kinds = [e[0] for e in events]
        # 只有一份 meta，无第二次整流
        assert kinds.count("meta") == 1
        tokens = [e[1] for e in events if e[0] == "token"]
        assert tokens[0] == "半截答"
        assert "中断" in tokens[-1]  # 固定收尾话术
        assert kinds[-1] == "done"
        # 拼接防护：full 累加里不得出现 fallback 重答内容
        assert len(tokens) == 2

    def test_reflow_allowed_before_any_tokens(self):
        """未发过 token 就抛异常 → 保留原 V1.2 整流 fallback 行为"""
        def _broken_early(*a, **k):
            raise RuntimeError("启动即炸")
            yield  # pragma: no cover（保持 generator 类型）

        fake_fallback = [("meta", {"intent": "policy_query"}),
                         ("token", "fallback 完整答案"), ("done", {"answer": "x"})]
        with patch("app.services.intent_service.IntentService.classify",
                   return_value=_policy_intent()), \
             patch.object(Synthesizer, "_handle_policy", staticmethod(_broken_early)), \
             patch("app.services.chat.orchestrator.v12_rag_run_stream",
                   return_value=iter(fake_fallback)) as mock_v12:
            events = list(Synthesizer.run_stream("怎么退货", user_id=1))

        mock_v12.assert_called_once()
        assert any(e[0] == "token" and "fallback" in e[1] for e in events)
