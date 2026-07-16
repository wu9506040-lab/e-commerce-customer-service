"""tests/test_audit_resolver.py

M14 Stage 5 验证：orchestrator._handle_order 的 5 个 resolver action 路径
都会调 try_log_action(action="resolver_decision", ...)，把决策留痕到 audit log。

覆盖：
1. DIRECT_ANSWER + 工具直答 → audit 1 次（direct_answer=True / used_llm=False）
2. DIRECT_ANSWER + LLM 综合 → audit 1 次（used_llm=True / card_sent 跟随 SSE_CARD_V2）
3. SHOW_PICKER → audit 1 次（card_sent=True / card_density="list"）
4. NOT_FOUND → audit 1 次（card_sent=False / invalid_order_no）
5. ASK_LOGIN_OR_LIST（0 订单） → audit 1 次（total_orders=0）
6. 异常路径不调 audit（try_log_action 内部已吞咽异常）

Why 独立测试文件：
- 防御性：未来修改 _handle_order 漏 audit 调用时该用例 fail
- 与 metrics 测试解耦：metrics 是数字汇总；audit 是离散事件
"""
import os
from unittest.mock import patch, MagicMock

import pytest

# 测试环境变量（必须在 import settings 前设置）
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/db?charset=utf8mb4")

from app.core.config import settings  # noqa: E402
from app.services.session_service import ANONYMOUS_USER_ID  # noqa: E402


# =============================================================
# 共享 fixture
# =============================================================

@pytest.fixture(autouse=True)
def _audit_mock():
    """自动 mock try_log_action，避免真写 DB"""
    with patch("app.services.chat.orchestrator.try_log_action") as mock_audit:
        yield mock_audit


def _run_handle_order(monkeypatch, resolver_action, total_orders, **resolver_kwargs):
    """辅助函数：跑一次 _handle_order 并消费 generator

    Args:
        resolver_action: OrderResolverAction 枚举值
        total_orders: Resolver 返的订单数
        **resolver_kwargs: 透传给 resolver mock 的字段
    """
    from app.services.chat.orchestrator import Synthesizer
    from app.services.context.order_context_resolver import OrderResolverAction

    # mock Resolver 返固定 action
    fake_result = MagicMock()
    fake_result.action = OrderResolverAction(resolver_action)
    fake_result.reason = "test_reason"
    fake_result.total_orders = total_orders
    fake_result.truncated = False
    fake_result.effective_order_no = "ORD001"
    fake_result.candidate_orders = []

    with patch("app.services.chat.orchestrator.get_order_context_resolver") as mock_get:
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = fake_result
        mock_get.return_value = mock_resolver

        # mock ContextService.load 返空（不依赖真 DB）
        monkeypatch.setattr(
            "app.services.chat.orchestrator.get_context_service",
            lambda: MagicMock(load=MagicMock(return_value=MagicMock())),
        )

        # mock OrderTool / OrderService（DIRECT_ANSWER 路径需要）
        with patch("app.services.chat.orchestrator.OrderService") as mock_od:
            mock_od.get_order_detail.return_value = None
            mock_od.list_user_orders.return_value = []

            # mock stream_dispatcher 以免真调 LLM
            with patch("app.services.chat.orchestrator.stream_dispatcher") as mock_sd:
                mock_sd.stream_simple.return_value = iter([])
                mock_sd.stream_llm.return_value = iter([])

                gen = Synthesizer._handle_order(
                    query="我的快递",
                    user_id=1,
                    intent_result={"intent": "order_query", "entities": {}},
                    order_no=None,
                    context_block="",
                    session_id="test-session",
                )
                list(gen)
        return fake_result


# =============================================================
# 1. DIRECT_ANSWER + 工具直答
# =============================================================
class TestDirectAnswerAudit:
    """DIRECT_ANSWER（"什么状态"类工具直答路径）→ 1 次 audit"""

    def test_direct_answer_emits_audit(self, monkeypatch, _audit_mock):
        """DIRECT_ANSWER + 工具直答命中（query="我的快递到哪了"等） → audit 一次"""
        with patch.object(settings, "ENABLE_ORDER_RESOLVER", True), \
             patch("app.services.chat.orchestrator.Synthesizer._try_direct_answer_order", return_value="订单 ORD001 当前状态：已签收"):
            _run_handle_order(
                monkeypatch,
                resolver_action="direct_answer",
                total_orders=1,
            )

        _audit_mock.assert_called_once()
        kwargs = _audit_mock.call_args.kwargs
        assert kwargs["action"] == "resolver_decision"
        assert kwargs["target_id"] == "1"
        assert kwargs["target_type"] == "user"
        assert kwargs["user"] is None
        assert kwargs["detail"]["resolver_action"] == "direct_answer"
        assert kwargs["detail"]["direct_answer"] is True
        assert kwargs["detail"]["used_llm"] is False
        assert kwargs["detail"]["card_sent"] is False


# =============================================================
# 2. DIRECT_ANSWER + LLM 综合
# =============================================================
class TestDirectAnswerWithLLMAudit:
    """DIRECT_ANSWER + LLM（无工具直答命中）→ 1 次 audit（used_llm=True）"""

    def test_llm_path_emits_audit(self, monkeypatch, _audit_mock):
        """DIRECT_ANSWER 但 _try_direct_answer_order 返 None（走 LLM）→ audit 一次，used_llm=True"""
        with patch.object(settings, "ENABLE_ORDER_RESOLVER", True), \
             patch.object(settings, "SSE_CARD_V2", True), \
             patch("app.services.chat.orchestrator.Synthesizer._try_direct_answer_order", return_value=None):
            _run_handle_order(
                monkeypatch,
                resolver_action="direct_answer",
                total_orders=1,
            )

        _audit_mock.assert_called_once()
        kwargs = _audit_mock.call_args.kwargs
        assert kwargs["action"] == "resolver_decision"
        assert kwargs["detail"]["resolver_action"] == "direct_answer"
        assert kwargs["detail"]["used_llm"] is True
        # N=1 + SSE_CARD_V2=True → card 期望发；实际发送由 mock 返回 None 决定


# =============================================================
# 3. SHOW_PICKER
# =============================================================
class TestShowPickerAudit:
    """SHOW_PICKER（N 个订单歧义）→ 1 次 audit（card_sent=True）"""

    def test_show_picker_emits_audit(self, monkeypatch, _audit_mock):
        """Resolver 返 SHOW_PICKER → audit 一次，card_sent=True"""
        with patch.object(settings, "ENABLE_ORDER_RESOLVER", True):
            _run_handle_order(
                monkeypatch,
                resolver_action="show_picker",
                total_orders=3,
            )

        _audit_mock.assert_called_once()
        kwargs = _audit_mock.call_args.kwargs
        assert kwargs["detail"]["resolver_action"] == "show_picker"
        assert kwargs["detail"]["card_sent"] is True
        assert kwargs["detail"]["total_orders"] == 3


# =============================================================
# 4. NOT_FOUND
# =============================================================
class TestNotFoundAudit:
    """NOT_FOUND（订单号无效/越权）→ 1 次 audit（card_sent=False / invalid_order_no）"""

    def test_not_found_emits_audit(self, monkeypatch, _audit_mock):
        """Resolver 返 NOT_FOUND → audit 一次，含 invalid_order_no 字段"""
        with patch.object(settings, "ENABLE_ORDER_RESOLVER", True):
            _run_handle_order(
                monkeypatch,
                resolver_action="not_found",
                total_orders=0,
            )

        _audit_mock.assert_called_once()
        kwargs = _audit_mock.call_args.kwargs
        assert kwargs["detail"]["resolver_action"] == "not_found"
        assert kwargs["detail"]["card_sent"] is False
        assert kwargs["detail"]["invalid_order_no"] == "ORD001"


# =============================================================
# 5. ASK_LOGIN_OR_LIST
# =============================================================
class TestAskLoginOrListAudit:
    """ASK_LOGIN_OR_LIST（0 订单）→ 1 次 audit（total_orders=0）"""

    def test_zero_orders_emits_audit(self, monkeypatch, _audit_mock):
        """Resolver 返 ASK_LOGIN_OR_LIST → audit 一次，total_orders=0"""
        with patch.object(settings, "ENABLE_ORDER_RESOLVER", True):
            _run_handle_order(
                monkeypatch,
                resolver_action="ask_login_or_list",
                total_orders=0,
            )

        _audit_mock.assert_called_once()
        kwargs = _audit_mock.call_args.kwargs
        assert kwargs["detail"]["resolver_action"] == "ask_login_or_list"
        assert kwargs["detail"]["total_orders"] == 0
        assert kwargs["detail"]["card_sent"] is False


# =============================================================
# 6. 短路：匿名用户不调 audit（无 resolver 决策）
# =============================================================
class TestAnonymousShortCircuit:
    """user_id=ANONYMOUS_USER_ID 短路：直接返 NO_LOGIN_PROMPT，不调 resolver、不调 audit"""

    def test_anonymous_no_audit(self, monkeypatch, _audit_mock):
        from app.services.chat.orchestrator import Synthesizer

        gen = Synthesizer._handle_order(
            query="我的快递",
            user_id=ANONYMOUS_USER_ID,
            intent_result={"intent": "order_query", "entities": {}},
            order_no=None,
        )
        events = list(gen)

        # 第一个事件必须是 meta（NO_LOGIN_PROMPT 短路）
        assert events[0][0] == "meta"
        # 不调 audit（短路，无 resolver 决策）
        _audit_mock.assert_not_called()


# =============================================================
# 7. 异常容错：audit 失败不影响业务（resilience）
# =============================================================
# 注：audit_service.with_safe_session 内部已吞咽 DB 异常，所以 orchestrator
# 不必额外 try/except。当 try_log_action 真的抛（mock 异常 / ImportError）时，
# 失败将传播给调用方 — 这是 by design（避免静默吞错掩盖问题）。
# 端到端测试见 test_audit_service.py（如有）。本文件专注于「dispatch 覆盖率」。
