"""
XianyuEscalationDetector 测试 — 转人工规则

按 CLAUDE.md 调试铁律：纯逻辑测试，不连 MCP
"""
import pytest

from app.services.xianyu.escalation_detector import (
    XianyuEscalationDetector,
    EscalationReason,
)


# =============================================================
# 1. 明确要求转人工
# =============================================================
class TestExplicitEscalation:
    def test_phrase_转人工(self):
        """'转人工' → 转人工"""
        d = XianyuEscalationDetector()
        result = d.should_escalate(unb="u1", message="转人工")
        assert result.escalate is True
        assert result.reason == EscalationReason.USER_REQUEST

    def test_phrase_找老板(self):
        """'找老板' → 转人工"""
        d = XianyuEscalationDetector()
        result = d.should_escalate(unb="u1", message="叫你们老板来")
        assert result.escalate is True
        assert result.reason == EscalationReason.USER_REQUEST


# =============================================================
# 2. 投诉/举报/12315 → 转人工
# =============================================================
class TestComplaintEscalation:
    @pytest.mark.parametrize("phrase", [
        "我要投诉",
        "向 12315 投诉",
        "准备找消协",
        "315 投诉你们",
        "准备举报",
    ])
    def test_complaint_phrase(self, phrase):
        """投诉 / 举报 / 12315 / 消协 / 315 → 转人工"""
        d = XianyuEscalationDetector()
        result = d.should_escalate(unb="u1", message=phrase)
        assert result.escalate is True
        assert result.reason == EscalationReason.COMPLAINT


# =============================================================
# 3. 严重指控 → 转人工
# =============================================================
class TestSevereAccusation:
    @pytest.mark.parametrize("phrase", [
        "你们欺诈",
        "卖假货",
        "骗人的",
        "黑店",
        "这是假货",
    ])
    def test_severe_accusation(self, phrase):
        """欺诈/假货/骗人/黑店 → 转人工"""
        d = XianyuEscalationDetector()
        result = d.should_escalate(unb="u1", message=phrase)
        assert result.escalate is True
        assert result.reason == EscalationReason.SEVERE_ACCUSATION


# =============================================================
# 4. 退款（所有变体）→ 转人工
# =============================================================
class TestRefundEscalation:
    @pytest.mark.parametrize("phrase", [
        "我要退款",
        "申请退款",
        "怎么退款",
        "退钱给我",
        "退款流程",
    ])
    def test_refund_phrase(self, phrase):
        """所有'退款'类话术 → 转人工（V16.2 不做退款场景）"""
        d = XianyuEscalationDetector()
        result = d.should_escalate(unb="u1", message=phrase)
        assert result.escalate is True
        assert result.reason == EscalationReason.REFUND


# =============================================================
# 5. 正常咨询不转人工
# =============================================================
class TestNormalPass:
    @pytest.mark.parametrize("phrase", [
        "你好在吗",
        "这个还能便宜吗",
        "包邮吗",
        "什么时候发货",
        "质量怎么样",
        "我想要",
    ])
    def test_normal_query_not_escalate(self, phrase):
        """正常售前/售中/售后 → 不转人工"""
        d = XianyuEscalationDetector()
        result = d.should_escalate(unb="u1", message=phrase)
        assert result.escalate is False, f"应不转人工：{phrase}"


# =============================================================
# 6. 情绪对抗（"不满意" 多次）
# =============================================================
class TestEmotionEscalation:
    def test_first_unsatisfied_not_escalate(self):
        """单次'不满意'不立刻转人工（给 Agent 修复机会）"""
        d = XianyuEscalationDetector()
        result = d.should_escalate(unb="u1", message="这个回答我不满意")
        assert result.escalate is False

    def test_two_consecutive_unsatisfied_escalate(self):
        """连续 2 次'不满意' → 转人工"""
        d = XianyuEscalationDetector()
        d.record_unsatisfied(unb="u1")
        result = d.should_escalate(unb="u1", message="还是不满意")
        assert result.escalate is True
        assert result.reason == EscalationReason.EMOTION


# =============================================================
# 7. 异常行为（同 unb 24h 5+ 次）
# =============================================================
class TestAbnormalBehavior:
    def test_under_threshold_not_escalate(self):
        """24h 内 < 5 次 → 不转"""
        d = XianyuEscalationDetector()
        for _ in range(4):
            d.record_message(unb="u1")
        result = d.should_escalate(unb="u1", message="在吗")
        assert result.escalate is False

    def test_over_threshold_escalate(self):
        """24h 内 >= 30 次 → 转（阈值 V18 调参 5→30）"""
        d = XianyuEscalationDetector()
        for _ in range(30):
            d.record_message(unb="u1")
        result = d.should_escalate(unb="u1", message="在吗")
        assert result.escalate is True
        assert result.reason == EscalationReason.ABNORMAL_BEHAVIOR

    def test_different_unb_independent(self):
        """不同 unb 各自计数"""
        d = XianyuEscalationDetector()
        for _ in range(5):
            d.record_message(unb="u1")
        result = d.should_escalate(unb="u2", message="在吗")
        assert result.escalate is False


# =============================================================
# 8. 转人工后的话术
# =============================================================
class TestEscalationReply:
    def test_get_reply_text(self):
        """转人工后给买家的话术"""
        d = XianyuEscalationDetector()
        reply = d.get_escalation_reply(reason=EscalationReason.COMPLAINT)
        assert reply  # 非空
        # 应承诺转人工/专员/客服
        assert any(
            kw in reply
            for kw in ["人工", "专员", "客服", "稍候"]
        )


# =============================================================
# 9. EscalationResult 数据类
# =============================================================
class TestEscalationResult:
    def test_result_fields(self):
        """EscalationResult 有 escalate + reason + reply"""
        d = XianyuEscalationDetector()
        result = d.should_escalate(unb="u1", message="投诉")
        assert hasattr(result, "escalate")
        assert hasattr(result, "reason")
        assert hasattr(result, "reply")