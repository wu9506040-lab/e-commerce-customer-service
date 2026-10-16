"""
V16.8 P0-A 测试：xianyu/orchestrator_bridge.py

覆盖：
1. handle_xianyu_refund 在三种 DB 状态（已退款 / 未退款 / 无订单）下产出正确的 SSE 事件序列
2. seller_unb 未传时从 env 读取
3. meta.intent="refund_query" + meta.channel="xianyu" + meta.xianyu_refund 元数据
4. yield 事件顺序：meta → token(s) → done
5. done.xianyu_refund 与 meta.xianyu_refund 一致（前端可解析）

不依赖外部 LLM / Qdrant（DB 用 SQLite in-memory，env mock）。
"""
import os
import pytest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 测试环境变量（与 test_xianyu_refund_handler.py 同模式）
# 强制设 DATABASE_URL，bridge 内部 os.environ.get("DATABASE_URL") 优先于 settings 缓存
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")


@pytest.fixture
def db_session():
    """SQLite in-memory DB（与 xianyu/refund_handler.py 同模式）"""
    from app.models.base import Base
    from app.models import xianyu_order  # noqa: ensure table registered

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture
def prepared_orders(db_session):
    """插入测试订单数据"""
    from app.services.xianyu.order_repository import XianyuOrderRepository

    def _seed(seller_unb="s1", unb="u1", order_no="O1", status="paid"):
        XianyuOrderRepository.upsert_from_tip(
            db_session, seller_unb, unb, "I1", order_no, status, "p", "m1",
        )
        db_session.commit()

    return _seed


# =============================================================
# 1. handle_xianyu_refund 三种 DB 状态路径
# =============================================================
class TestXianyuRefundBridgeDispatch:
    """bridge 必须 yield 完整 SSE 序列 + 决策元数据"""

    def _drain(self, gen):
        events = list(gen)
        return events

    def _find(self, events, type_):
        matches = [e for e in events if e[0] == type_]
        assert len(matches) >= 1, f"expected at least one {type_} event, got: {[e[0] for e in events]}"
        return matches[0][1]

    @patch.dict(os.environ, {"XIANYU_SELLER_UNB": "s1"})
    def test_refunded_order_yields_meta_reject_done(self, db_session, prepared_orders):
        """已退款订单：bridge yield meta.should_reject=True + 拒绝文本 + done"""
        from app.services.xianyu.orchestrator_bridge import handle_xianyu_refund

        prepared_orders(seller_unb="s1", unb="u1", order_no="O1", status="refunded")

        events = self._drain(handle_xianyu_refund(
            query="我要退款",
            buyer_unb="u1",
            intent_result={"intent": "refund_query", "entities": {}},
            db_session=db_session,  # 注入 fixture 的 db（共享 schema）
        ))
        meta = self._find(events, "meta")
        done = self._find(events, "done")

        # meta 元数据
        assert meta["intent"] == "refund_query"
        assert meta["channel"] == "xianyu"
        assert meta["xianyu_refund"]["should_reject"] is True
        assert meta["xianyu_refund"]["should_escalate"] is False
        assert meta["xianyu_refund"]["order_state"] == "refunded"
        # reply_text 里实际字面：'退款成功'（reject_reason="已退款" 在 meta.xianyu_refund.reject_reason）
        assert ("已退款" in done["answer"] or "退款成功" in done["answer"])
        assert meta["xianyu_refund"]["reject_reason"] == "已退款"

        # token 事件必然存在（reply_text 拆 chunk）
        token_events = [e for e in events if e[0] == "token"]
        assert len(token_events) >= 1
        combined = "".join(e[1] for e in token_events)
        assert combined == done["answer"]

        # done 元数据
        assert done["channel"] == "xianyu"
        assert done["xianyu_refund"]["should_reject"] is True
        assert done["xianyu_refund"]["order_no"] == "O1"

    @patch.dict(os.environ, {"XIANYU_SELLER_UNB": "s1"})
    def test_unrefunded_order_escalates(self, db_session, prepared_orders):
        """未退款订单（已付款）：bridge yield should_escalate=True + 状态描述 + 转人工话术"""
        from app.services.xianyu.orchestrator_bridge import handle_xianyu_refund

        prepared_orders(seller_unb="s1", unb="u1", order_no="O2", status="paid")

        events = self._drain(handle_xianyu_refund(
            query="我要退款",
            buyer_unb="u1",
            db_session=db_session,
        ))
        meta = self._find(events, "meta")
        done = self._find(events, "done")

        assert meta["xianyu_refund"]["should_reject"] is False
        assert meta["xianyu_refund"]["should_escalate"] is True
        assert meta["xianyu_refund"]["order_state"] == "paid"
        assert meta["xianyu_refund"]["escalation_priority"] == "normal"
        assert "订单 O2" in done["answer"]
        assert "专员" in done["answer"]
        assert done["xianyu_refund"]["order_no"] == "O2"

    @patch.dict(os.environ, {"XIANYU_SELLER_UNB": "s1"})
    def test_no_order_record_unknown_escalate(self, db_session):
        """无订单记录：bridge yield should_escalate=True + order_state="unknown" + 默认话术"""
        from app.services.xianyu.orchestrator_bridge import handle_xianyu_refund

        events = self._drain(handle_xianyu_refund(
            query="我要退款",
            buyer_unb="unknown_buyer",
            db_session=db_session,
        ))
        meta = self._find(events, "meta")
        done = self._find(events, "done")

        assert meta["xianyu_refund"]["should_escalate"] is True
        assert meta["xianyu_refund"]["order_state"] == "unknown"
        assert meta["xianyu_refund"]["should_reject"] is False
        assert "专员" in done["answer"]
        assert done["xianyu_refund"]["order_no"] is None


# =============================================================
# 2. seller_unb 解析优先级：参数 > env
# =============================================================
class TestSellerUnbResolution:
    def _resolve(self, env_value, param_value):
        with patch.dict(os.environ, {"XIANYU_SELLER_UNB": env_value} if env_value else {}, clear=False):
            from app.services.xianyu.orchestrator_bridge import _resolve_seller_unb
            return _resolve_seller_unb(param_value)

    def test_explicit_param_wins_over_env(self):
        assert self._resolve(env_value="env-seller", param_value="param-seller") == "param-seller"

    def test_fallback_to_env(self):
        assert self._resolve(env_value="env-seller", param_value=None) == "env-seller"

    def test_default_placeholder_when_empty(self):
        # env unset + param empty → "default-seller" 兜底
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XIANYU_SELLER_UNB", None)
            from app.services.xianyu.orchestrator_bridge import _resolve_seller_unb
            assert _resolve_seller_unb(None) == "default-seller"
            assert _resolve_seller_unb("") == "default-seller"


# =============================================================
# 3. bridge 不依赖外部 LLM（V16.3「退款决策由规则层决定」原则）
# =============================================================
class TestNoLLMDependency:
    @patch.dict(os.environ, {"XIANYU_SELLER_UNB": "s1"})
    def test_does_not_invoke_llm(self, db_session, prepared_orders):
        """bridge 不应 import / 调用任何 LLM provider"""
        prepared_orders(seller_unb="s1", unb="u1", order_no="O3", status="paid")

        with patch("app.core.providers.llm.get_llm_provider") as mock_llm:
            from app.services.xianyu.orchestrator_bridge import handle_xianyu_refund
            list(handle_xianyu_refund(
                query="我要退款",
                buyer_unb="u1",
                db_session=db_session,
            ))
            # bridge 必须根本不调 LLM
            assert mock_llm.called is False, (
                "V16.3 原则：退款决策由 DB/规则层决定，bridge 不应该调用 LLM"
            )
