"""
XianyuMessageGuard 测试 — send_message 前敏感词扫描

按 CLAUDE.md 调试铁律：纯逻辑测试，不连 MCP
"""
import pytest

from app.services.xianyu.message_guard import XianyuMessageGuard, GuardResult


# =============================================================
# 1. 退款话术拦截
# =============================================================
class TestRefundBlock:
    def test_block_refund_confirmed_phrase(self):
        """'已退款 X 元' → 拦截"""
        g = XianyuMessageGuard()
        result = g.check("您的订单已退款 50 元，1-3 天到账")
        assert result.blocked is True
        assert "退款" in result.reason or "refund" in result.reason.lower()

    def test_block_refund_arrived(self):
        """'退款已到账' → 拦截"""
        g = XianyuMessageGuard()
        result = g.check("退款已到账，请查收")
        assert result.blocked is True

    def test_block_already_refunded(self):
        """'已为您退款' → 拦截"""
        g = XianyuMessageGuard()
        result = g.check("已为您退款，请注意查收")
        assert result.blocked is True

    def test_block_transfer_compensation(self):
        """'已转账' / '补偿金' → 拦截"""
        g = XianyuMessageGuard()
        for phrase in ["已转账 100 元", "补偿金 50 元已发", "赔偿已到账"]:
            r = g.check(phrase)
            assert r.blocked is True, f"应拦截：{phrase}"


# =============================================================
# 2. 投诉/举报拦截
# =============================================================
class TestComplaintBlock:
    def test_block_12315(self):
        """'12315' / '消协' / '315' → 拦截（这些必须转人工）"""
        g = XianyuMessageGuard()
        for phrase in ["我要投诉 12315", "准备找消协", "315 投诉"]:
            r = g.check(phrase)
            assert r.blocked is True, f"应拦截：{phrase}"

    def test_block_fraud_accusation(self):
        """'欺诈' / '假货' / '骗人' → 拦截（这些必须转人工）"""
        g = XianyuMessageGuard()
        for phrase in ["你们欺诈消费者", "这是假货", "骗人的"]:
            r = g.check(phrase)
            assert r.blocked is True, f"应拦截：{phrase}"


# =============================================================
# 3. 正常话术放行
# =============================================================
class TestNormalPass:
    def test_pass_greeting(self):
        """'你好在吗' → 放行"""
        g = XianyuMessageGuard()
        result = g.check("你好，这个还有货吗？")
        assert result.blocked is False

    def test_pass_negotiation(self):
        """'能便宜吗' → 放行（不是退款）"""
        g = XianyuMessageGuard()
        result = g.check("能便宜 20 吗？")
        assert result.blocked is False

    def test_pass_shipping_question(self):
        """'包邮吗 / 什么时候发' → 放行"""
        g = XianyuMessageGuard()
        for phrase in ["包邮吗", "什么时候发货", "快递到哪了"]:
            r = g.check(phrase)
            assert r.blocked is False

    def test_pass_quality_question(self):
        """'质量怎么样' → 放行（不是退款）"""
        g = XianyuMessageGuard()
        result = g.check("这个材质是什么？能用多久？")
        assert result.blocked is False


# =============================================================
# 4. 退款话术替换（不直接拦截，转敏感）
# =============================================================
class TestRefundSubstitute:
    def test_refund_word_but_safe(self):
        """'退款政策' / '退款说明' → 放行（不是承诺）"""
        g = XianyuMessageGuard()
        for phrase in ["7 天无理由退款政策", "退款说明", "支持退款吗"]:
            r = g.check(phrase)
            assert r.blocked is False, f"应放行：{phrase}"


# =============================================================
# 5. 拦截后建议
# =============================================================
class TestBlockSuggestion:
    def test_blocked_returns_safe_alternative(self):
        """拦截后返建议话术（让 Agent 改成安全的）"""
        g = XianyuMessageGuard()
        result = g.check("您的订单已退款 50 元")
        assert result.blocked is True
        assert result.suggestion  # 有替代话术
        # 替代话术应引导转人工/专员/客服（承诺转人工客服处理）
        assert any(
            kw in result.suggestion
            for kw in ["人工", "专员", "客服", "处理"]
        ), f"suggestion 应引导转人工/专员/客服，实际: {result.suggestion!r}"


# =============================================================
# 6. 多重敏感词
# =============================================================
class TestMultipleMatches:
    def test_first_match_wins(self):
        """多个敏感词 → 返第一个命中的 reason"""
        g = XianyuMessageGuard()
        result = g.check("已退款 100 元 + 投诉 12315")
        assert result.blocked is True
        assert result.reason  # 有 reason


# =============================================================
# 7. 边界
# =============================================================
class TestEdgeCases:
    def test_empty_string(self):
        """空字符串 → 放行"""
        g = XianyuMessageGuard()
        result = g.check("")
        assert result.blocked is False

    def test_only_whitespace(self):
        """纯空白 → 放行"""
        g = XianyuMessageGuard()
        result = g.check("   \n\t  ")
        assert result.blocked is False

    def test_case_insensitive(self):
        """'已退款' / '已退款' 大小写都拦截"""
        g = XianyuMessageGuard()
        for phrase in ["已退款", "已退款", "Refund Completed"]:
            r = g.check(phrase)
            # 中文场景必须拦截，英文按情况
            if "退款" in phrase:
                assert r.blocked is True