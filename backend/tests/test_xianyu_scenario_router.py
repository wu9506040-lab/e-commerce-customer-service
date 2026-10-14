"""
XianyuScenarioRouter 测试 — 售前/售中/售后场景路由

按 CLAUDE.md 调试铁律：纯逻辑测试
"""
import pytest

from app.services.xianyu.scenario_router import (
    XianyuScenarioRouter,
    Scenario,
    ScenarioResult,
)


# =============================================================
# 1. 售前：商品咨询
# =============================================================
class TestPresale:
    @pytest.mark.parametrize("msg", [
        "这个还能便宜吗",
        "在吗？",
        "包邮吗",
        "材质是什么",
        "质量怎么样",
        "还有货吗",
    ])
    def test_presale_query(self, msg):
        """商品咨询类 → 售前"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message=msg, is_returning_buyer=False)
        assert result.scenario == Scenario.PRESALE


# =============================================================
# 2. 售中：物流/地址/催促发货
# =============================================================
class TestInSale:
    @pytest.mark.parametrize("msg", [
        "什么时候发货",
        "快递到哪了",
        "可以改地址吗",
        "还没收到",
        "催促发货",
    ])
    def test_in_sale_query(self, msg):
        """物流/地址/催促 → 售中"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message=msg, is_returning_buyer=True)
        assert result.scenario == Scenario.IN_SALE


# =============================================================
# 3. 售后：换货/质量/评价（非退款）
# =============================================================
class TestAfterSale:
    @pytest.mark.parametrize("msg", [
        "可以换货吗",
        "我想换码",
        "质量有问题",
        "收到货了，满意",
        "给个好评",
    ])
    def test_after_sale_query(self, msg):
        """换货/质量/评价 → 售后"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message=msg, is_returning_buyer=True)
        assert result.scenario == Scenario.AFTER_SALE


# =============================================================
# 4. 退款：走 escalate（路由不到售后）
# =============================================================
class TestRefundGoesToEscalate:
    @pytest.mark.parametrize("msg", [
        "我要退款",
        "退款流程",
    ])
    def test_refund_returns_escalate(self, msg):
        """退款类 → escalate（V16.2 路由识别，但实际不处理，转人工）"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message=msg, is_returning_buyer=True)
        assert result.scenario == Scenario.ESCALATE
        assert result.should_escalate is True


# =============================================================
# 5. 老客识别影响售前判断
# =============================================================
class TestReturningBuyer:
    def test_returning_buyer_with_shipping_query(self):
        """老客问物流 → 售中"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message="快递到哪了", is_returning_buyer=True)
        assert result.scenario == Scenario.IN_SALE

    def test_new_buyer_with_presale_query(self):
        """新客问商品 → 售前"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u2", message="这个还能便宜吗", is_returning_buyer=False)
        assert result.scenario == Scenario.PRESALE

    def test_returning_buyer_asking_price(self):
        """老客问议价 → 售前（不是售中）"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message="再便宜点", is_returning_buyer=True)
        assert result.scenario == Scenario.PRESALE


# =============================================================
# 6. ScenarioResult 数据
# =============================================================
class TestScenarioResult:
    def test_result_has_prompt_hint(self):
        """ScenarioResult 包含给 LLM 的 hint（场景化指令）"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message="包邮吗", is_returning_buyer=False)
        # hint 应是中文场景提示
        assert "售前" in result.prompt_hint
        # 应给特色话术建议
        assert len(result.prompt_hint) > 20

    def test_result_for_after_sale_has_quality_focus(self):
        """售后的 hint 应聚焦质量/换货"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message="换货", is_returning_buyer=True)
        assert "售后" in result.prompt_hint


# =============================================================
# 7. 路由优先级（退款优先于场景分类）
# =============================================================
class TestRoutePriority:
    def test_refund_phrase_overrides_presale(self):
        """含'退款'的话术即使像是售前也走 escalate"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message="买之前问下能退款吗", is_returning_buyer=False)
        # 包含退款 → escalate
        assert result.scenario == Scenario.ESCALATE


# =============================================================
# 8. 边界
# =============================================================
class TestEdgeCases:
    def test_empty_message(self):
        """空消息 → 售前（默认）"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message="", is_returning_buyer=False)
        assert result.scenario == Scenario.PRESALE

    def test_unknown_message_presale(self):
        """看不懂的消息 → 默认售前（Agent 自由发挥）"""
        r = XianyuScenarioRouter()
        result = r.route(unb="u1", message="哈哈哈哈哈", is_returning_buyer=False)
        assert result.scenario == Scenario.PRESALE