"""
V16.8 P0-A 测试：orchestrator.run_stream refund_query channel 分支

覆盖：
1. channel="xianyu" → 路由到 handle_xianyu_refund（不调商城 handle_refund_v3/v2）
2. channel=None 或非 xianyu → 走原 V3/V2 路径（兼容商城前端）

不依赖外部 LLM（mock bridge）；db 用 SQLite 或 mock 全部。
"""
import os
from unittest.mock import patch, MagicMock

import pytest

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


def _collect(gen):
    """把 generator 展开成 list[event]"""
    return list(gen)


# =============================================================
# 1. channel=xianyu → xianyu bridge（不调商城 handle_refund_v3/v2）
# =============================================================
class TestRefundChannelBranch:

    @patch("app.services.xianyu.orchestrator_bridge.handle_xianyu_refund")
    @patch("app.services.chat.refund_handler.handle_refund_v3")
    @patch("app.services.chat.refund_handler.handle_refund_v2")
    def test_xianyu_channel_routes_to_bridge(self, mock_v2, mock_v3, mock_bridge):
        """channel=xianyu → handle_xianyu_refund, 不调 v2/v3"""
        from app.services.chat.orchestrator import Synthesizer

        # mock bridge 返回一个含 meta/token/done 的 generator
        def fake_bridge(query, buyer_unb, intent_result, **kwargs):
            yield ("meta", {"intent": "refund_query", "channel": "xianyu"})
            yield ("token", "已退款啦")
            yield ("done", {"answer": "已退款啦", "channel": "xianyu"})
        mock_bridge.side_effect = fake_bridge

        # 强制 primary=refund_query：mock IntentService.classify
        with patch.object(
            __import__("app.services.intent_service", fromlist=["IntentService"]).IntentService,
            "classify",
            return_value={
                "intents": [{"intent": "refund_query", "confidence": 1.0}],
                "primary": "refund_query",
                "method": "rule",
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "intent": "refund_query",
                "confidence": 1.0,
            },
        ):
            with patch.object(__import__("app.core.config", fromlist=["settings"]).settings,
                              "USE_LANGGRAPH_REFUND", False):
                events = _collect(Synthesizer.run_stream(
                    "我要退款",
                    user_id=0,
                    channel="xianyu",
                    buyer_unb="test-buyer",
                ))

        # xianyu bridge 必须被调
        assert mock_bridge.called, "channel=xianyu 应该路由到 handle_xianyu_refund"
        # 商城 v2/v3 绝对不能调
        assert not mock_v2.called, "channel=xianyu 不应该走商城 handle_refund_v2"
        assert not mock_v3.called, "channel=xianyu 不应该走商城 handle_refund_v3"

        # 验证 SSE 事件序列
        meta_events = [e for e in events if e[0] == "meta"]
        assert any(e[1].get("channel") == "xianyu" for e in meta_events), \
            "meta 事件必须含 channel=xianyu"

    @patch("app.services.xianyu.orchestrator_bridge.handle_xianyu_refund")
    @patch("app.services.chat.refund_handler.handle_refund_v3")
    def test_web_channel_routes_to_v3(self, mock_v3, mock_bridge):
        """channel=web（或 None）→ 走商城 V3（不调 xianyu bridge）"""
        from app.services.chat.orchestrator import Synthesizer

        def fake_v3(query, user_id, intent_result, **kwargs):
            yield ("meta", {"intent": "refund_query", "v3_engine": "v3"})
            yield ("token", "商城退款处理中")
            yield ("done", {"answer": "商城退款处理中"})
        mock_v3.side_effect = fake_v3

        with patch.object(
            __import__("app.services.intent_service", fromlist=["IntentService"]).IntentService,
            "classify",
            return_value={
                "intents": [{"intent": "refund_query", "confidence": 1.0}],
                "primary": "refund_query",
                "method": "rule",
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "intent": "refund_query",
                "confidence": 1.0,
            },
        ):
            with patch.object(__import__("app.core.config", fromlist=["settings"]).settings,
                              "ENABLE_BUSINESS_FLOW", False):
              with patch.object(__import__("app.core.config", fromlist=["settings"]).settings,
                              "USE_LANGGRAPH_REFUND", True):
                events = _collect(Synthesizer.run_stream(
                    "我要退款",
                    user_id=42,
                    channel="web",  # 商城 channel
                    buyer_unb=None,
                ))

        assert mock_v3.called, "channel=web 应该走商城 V3"
        assert not mock_bridge.called, "channel=web 不应该调 xianyu bridge"
        assert any(e[1].get("v3_engine") == "v3" for e in events if e[0] == "meta")

    @patch("app.services.xianyu.orchestrator_bridge.handle_xianyu_refund")
    @patch("app.services.chat.refund_handler.handle_refund_v3")
    def test_none_channel_defaults_to_v3(self, mock_v3, mock_bridge):
        """channel=None → 向后兼容走商城 V3"""
        from app.services.chat.orchestrator import Synthesizer

        def fake_v3(query, user_id, intent_result, **kwargs):
            yield ("meta", {"intent": "refund_query"})
            yield ("done", {"answer": "ok"})
        mock_v3.side_effect = fake_v3

        with patch.object(
            __import__("app.services.intent_service", fromlist=["IntentService"]).IntentService,
            "classify",
            return_value={
                "intents": [{"intent": "refund_query", "confidence": 1.0}],
                "primary": "refund_query",
                "method": "rule",
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "intent": "refund_query",
                "confidence": 1.0,
            },
        ):
            with patch.object(__import__("app.core.config", fromlist=["settings"]).settings,
                              "ENABLE_BUSINESS_FLOW", False):
              with patch.object(__import__("app.core.config", fromlist=["settings"]).settings,
                              "USE_LANGGRAPH_REFUND", True):
                _collect(Synthesizer.run_stream(
                    "我要退款", user_id=42,
                    # 不传 channel，保持向后兼容
                ))

        assert mock_v3.called, "channel=None 应该走商城 V3（向后兼容）"
        assert not mock_bridge.called
