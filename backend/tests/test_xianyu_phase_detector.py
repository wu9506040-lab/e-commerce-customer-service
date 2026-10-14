"""
PhaseDetector 测试 — 订单先于意图两阶段判定

按 CLAUDE.md 调试铁律：测试 mock OrderService + intent_service
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.xianyu.phase_detector import Phase, PhaseDetector


# =============================================================
# 1. 售前：无订单 + 售前问句
# =============================================================
class TestPresale:
    def test_no_order_presale_question(self):
        """无订单 + 问'在吗' → 售前"""
        detector = PhaseDetector(
            order_service=MagicMock(query_by_user_unb=MagicMock(return_value=[])),
            intent_classifier=MagicMock(classify=MagicMock(return_value={
                "primary": "policy_query", "method": "rule"
            })),
        )
        result = detector.detect_phase(unb="u1", last_message="在吗")
        assert result.phase == Phase.PRESALE
        assert result.ambiguous is False


# =============================================================
# 2. 售中：有进行中订单 + 问发货
# =============================================================
class TestInSale:
    def test_inprogress_order_shipment_question(self):
        """进行中订单 + 问'发货' → 售中"""
        mock_order = MagicMock()
        mock_order.query_by_user_unb = MagicMock(return_value=[
            {"order_no": "O1", "status": "shipped"}
        ])
        mock_intent = MagicMock()
        mock_intent.classify = MagicMock(return_value={
            "primary": "order_query", "method": "rule"
        })
        detector = PhaseDetector(
            order_service=mock_order,
            intent_classifier=mock_intent,
        )
        result = detector.detect_phase(unb="u1", last_message="什么时候发货")
        assert result.phase == Phase.IN_SALE
        assert result.ambiguous is False


# =============================================================
# 3. 售后：已签收订单 + 问退款
# =============================================================
class TestAfterSale:
    def test_completed_order_refund_question(self):
        """已签收订单 + 问'退款' → 售后"""
        mock_order = MagicMock()
        mock_order.query_by_user_unb = MagicMock(return_value=[
            {"order_no": "O1", "status": "completed"}
        ])
        mock_intent = MagicMock()
        mock_intent.classify = MagicMock(return_value={
            "primary": "refund_query", "method": "rule"
        })
        detector = PhaseDetector(
            order_service=mock_order,
            intent_classifier=mock_intent,
        )
        result = detector.detect_phase(unb="u1", last_message="怎么退款")
        assert result.phase == Phase.AFTER_SALE
        assert result.ambiguous is False


# =============================================================
# 4. 矛盾：老客问"在吗"（订单=已签收 但问的是新话题）
# =============================================================
class TestAmbiguous:
    def test_old_buyer_asks_price_ambiguous(self):
        """老客 + 问'在吗'（phase1=售后、phase2=售前）→ ambiguous"""
        mock_order = MagicMock()
        mock_order.query_by_user_unb = MagicMock(return_value=[
            {"order_no": "O1", "status": "completed"}
        ])
        mock_intent = MagicMock()
        mock_intent.classify = MagicMock(return_value={
            "primary": "policy_query", "method": "rule"
        })
        detector = PhaseDetector(
            order_service=mock_order,
            intent_classifier=mock_intent,
        )
        result = detector.detect_phase(unb="u1", last_message="在吗")
        assert result.phase == Phase.AFTER_SALE  # 取订单阶段（硬条件优先）
        assert result.ambiguous is True  # 但标记歧义


# =============================================================
# 5. 订单查询失败降级
# =============================================================
class TestOrderServiceDegradation:
    def test_order_service_error_falls_back_to_intent(self):
        """订单服务抛异常 → 仅用 intent 判定"""
        mock_order = MagicMock()
        mock_order.query_by_user_unb = MagicMock(side_effect=Exception("db down"))
        mock_intent = MagicMock()
        mock_intent.classify = MagicMock(return_value={
            "primary": "refund_query", "method": "llm"
        })
        detector = PhaseDetector(
            order_service=mock_order,
            intent_classifier=mock_intent,
        )
        result = detector.detect_phase(unb="u1", last_message="退款")
        assert result.phase == Phase.AFTER_SALE  # 靠 intent 兜底
        assert result.order_error != ""


# =============================================================
# 6. 订单状态映射
# =============================================================
class TestOrderStatusMapping:
    def test_pending_payment_is_presale(self):
        """未付款订单 → 售前（不是售中）"""
        mock_order = MagicMock()
        mock_order.query_by_user_unb = MagicMock(return_value=[
            {"order_no": "O1", "status": "pending_payment"}
        ])
        mock_intent = MagicMock()
        mock_intent.classify = MagicMock(return_value={
            "primary": "product_query", "method": "rule"
        })
        detector = PhaseDetector(
            order_service=mock_order,
            intent_classifier=mock_intent,
        )
        result = detector.detect_phase(unb="u1", last_message="在吗")
        assert result.phase == Phase.PRESALE  # 未付款=售前

    def test_shipped_is_in_sale(self):
        """shipped → 售中"""
        mock_order = MagicMock()
        mock_order.query_by_user_unb = MagicMock(return_value=[
            {"order_no": "O1", "status": "shipped"}
        ])
        mock_intent = MagicMock()
        mock_intent.classify = MagicMock(return_value={
            "primary": "policy_query", "method": "rule"
        })
        detector = PhaseDetector(
            order_service=mock_order,
            intent_classifier=mock_intent,
        )
        result = detector.detect_phase(unb="u1", last_message="在吗")
        assert result.phase == Phase.IN_SALE

    def test_cancelled_treated_as_presale(self):
        """已取消订单 → 售前（不再算老客）"""
        mock_order = MagicMock()
        mock_order.query_by_user_unb = MagicMock(return_value=[
            {"order_no": "O1", "status": "cancelled"}
        ])
        mock_intent = MagicMock()
        mock_intent.classify = MagicMock(return_value={
            "primary": "policy_query", "method": "rule"
        })
        detector = PhaseDetector(
            order_service=mock_order,
            intent_classifier=mock_intent,
        )
        result = detector.detect_phase(unb="u1", last_message="在吗")
        assert result.phase == Phase.PRESALE  # 已取消=无订单=售前


# =============================================================
# 7. to_prompt_context
# =============================================================
class TestPromptContext:
    def test_includes_phase_label(self):
        """to_prompt_context 返中文阶段标签"""
        mock_order = MagicMock()
        mock_order.query_by_user_unb = MagicMock(return_value=[])
        mock_intent = MagicMock()
        mock_intent.classify = MagicMock(return_value={"primary": "policy_query"})
        detector = PhaseDetector(order_service=mock_order, intent_classifier=mock_intent)
        result = detector.detect_phase(unb="u1", last_message="在吗")
        ctx = result.to_prompt_context()
        assert "售前" in ctx