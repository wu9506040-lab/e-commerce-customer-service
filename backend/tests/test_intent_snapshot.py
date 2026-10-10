"""V13（2.3，2026-10-11）：意图快照落库地基测试

覆盖：
1. run_stream 首发 _intent 内部事件（chat.py 消费、不外发）
2. A4 升级链路的 upgraded 标记透传到快照（真实规则，无 mock 分类）
3. metrics 方法分布/升级计数入 snapshot
4. persist_to_mysql 携带 intent_snapshot 入 assistant 行（fake session）
5. 参数不完整时 persist 返回 None（新契约：返回值 Optional[int]）
"""
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x")

from app.services.intent_service import IntentService
from app.services.metrics import metrics


class TestIntentSnapshotEvents:

    def test_run_stream_yields_intent_first(self):
        from app.services.chat.orchestrator import Synthesizer

        with patch("app.services.intent_service.IntentService.classify",
                   return_value={
                       "intents": [{"intent": "policy_query", "confidence": 0.9}],
                       "primary": "policy_query", "intent": "policy_query",
                       "method": "llm", "confidence": 0.9,
                       "entities": {"order_no": None, "sku": None, "keywords": []},
                   }), \
             patch.object(Synthesizer, "_handle_policy",
                          staticmethod(lambda *a, **k: iter([("done", {"answer": ""})]))):
            events = list(Synthesizer.run_stream("随便问问", user_id=1))

        assert events[0][0] == "_intent"
        snap = events[0][1]
        assert snap["primary"] == "policy_query"
        assert snap["method"] == "llm"
        assert snap["upgraded"] is False

    def test_upgrade_flag_flows_to_result(self):
        """A4 真实规则链路：升级发生 → result['upgraded']=True（快照与计数共用）"""
        before = metrics.intent_upgrade_total
        r = IntentService.classify("ORD20260628004 怎么申请退款，流程是什么")
        assert r["primary"] == "refund_query"
        assert r.get("upgraded") is True
        assert metrics.intent_upgrade_total == before + 1
        assert metrics.intent_by_method.get("rule", 0) >= 1


class TestMetricsBlock:

    def test_snapshot_intent_block(self):
        metrics.inc_intent_method("rule")
        metrics.inc_intent_method("llm")
        snap = metrics.snapshot()
        assert "intent" in snap
        assert snap["intent"]["by_method"].get("llm", 0) >= 1
        assert "upgrade_total" in snap["intent"]


class TestPersistCarriesSnapshot:

    def test_intent_snapshot_written_to_assistant_row(self):
        from app.services import mysql_store

        added = []

        class _FakeSession:
            def execute(self, *a, **k):
                m = MagicMock()
                m.scalar_one_or_none.return_value = None  # 新会话分支
                return m

            def add(self, obj):
                added.append(obj)

            def flush(self):
                pass

        @contextmanager
        def fake_session(commit=False):
            yield _FakeSession()

        snap = {"primary": "policy_query", "method": "rule",
                "confidence": 1.0, "intents": [], "upgraded": False}
        with patch.object(mysql_store, "with_safe_session", fake_session):
            ret = mysql_store.persist_to_mysql(
                "S-snap", 42, "退货政策", "7 天无理由",
                ["doc"], [0.9], intent_snapshot=snap,
            )

        assistant = [m for m in added if getattr(m, "role", None) == "assistant"]
        assert len(assistant) == 1
        assert assistant[0].intent_snapshot == snap
        assert ret is None or isinstance(ret, int)  # fake flush 无自增 id → None 契约

    def test_incomplete_params_returns_none(self):
        from app.services.mysql_store import persist_to_mysql
        assert persist_to_mysql("", 1, "q", "a") is None

    def test_session_facade_forwards_snapshot_and_id(self):
        """V13(2.3) 回归锁：chat.py 实际调用的是 session_service 门面——
        首版事故（门面没收新参 TypeError 被吞成 aid=None）绝不允许复发"""
        import inspect

        from app.services import session_service

        sig = inspect.signature(session_service.persist_to_mysql)
        assert "intent_snapshot" in sig.parameters
        assert sig.return_annotation is not inspect.Signature.empty  # 有返回契约

        captured = {}

        def fake_store(**kw):
            captured.update(kw)
            return 12345

        with patch("app.services.session_service.mysql_store.persist_to_mysql",
                   side_effect=fake_store):
            aid = session_service.persist_to_mysql(
                "S-facade", 7, "q", "a",
                contexts=["c"], scores=[0.9],
                intent_snapshot={"primary": "policy_query", "method": "rule"},
            )
        assert aid == 12345  # 返回值必须透传（done 事件带 id 的链路源头）
        assert captured["intent_snapshot"]["method"] == "rule"
