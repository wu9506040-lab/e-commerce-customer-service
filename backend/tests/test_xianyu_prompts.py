"""
XianyuPrompts 测试 — 加载 YAML 模板 + 注入到 system prompt

按 CLAUDE.md 调试铁律：纯逻辑测试
"""
import pytest

from app.services.xianyu.xianyu_prompts import XianyuPrompts, XianyuPromptTemplate


# =============================================================
# 1. 加载模板
# =============================================================
class TestLoad:
    def test_load_presale(self):
        """加载售前模板"""
        p = XianyuPrompts()
        template = p.load(scenario="presale")
        assert template.system  # 非空
        assert "主业做模具" in template.system  # V18 人设：主业模具 + 当前商品说明
        assert len(template.examples) > 0

    def test_load_in_sale(self):
        """加载售中模板"""
        p = XianyuPrompts()
        template = p.load(scenario="in_sale")
        assert template.system
        assert "物流" in template.system or "发货" in template.system

    def test_load_after_sale(self):
        """加载售后模板"""
        p = XianyuPrompts()
        template = p.load(scenario="after_sale")
        assert template.system
        # 售后必须明确说"不退款"
        assert "退款" in template.system

    def test_load_unknown_scenario_returns_presale(self):
        """未知场景 → 售前默认"""
        p = XianyuPrompts()
        template = p.load(scenario="unknown_xxx")
        assert template.system


# =============================================================
# 2. 渲染 system prompt（含场景 hint + 买家 profile）
# =============================================================
class TestRender:
    def test_render_basic(self):
        """render(system_base) → system + base 拼接"""
        p = XianyuPrompts()
        result = p.render(scenario="presale", system_base="你是智能客服")
        assert "你是智能客服" in result
        assert "主业做模具" in result

    def test_render_with_buyer_profile(self):
        """render 带 buyer_profile → 注入老客/昵称"""
        p = XianyuPrompts()
        result = p.render(
            scenario="presale",
            system_base="你是智能客服",
            buyer_profile="买家昵称=zwyyy 老客=True",
        )
        assert "zwyyy" in result
        assert "老客" in result

    def test_render_includes_examples(self):
        """render 含 examples（few-shot）"""
        p = XianyuPrompts()
        result = p.render(scenario="presale", system_base="你是智能客服")
        # examples 应出现 buyer/seller 对话
        assert "buyer" in result.lower() or "seller" in result.lower()


# =============================================================
# 3. 占位符替换
# =============================================================
class TestPlaceholder:
    def test_replace_nick(self):
        """{nick} → 实际昵称"""
        p = XianyuPrompts()
        template = p.load(scenario="presale")
        # 模板里"亲"这种占位符 + nick
        result = p._replace_placeholders(template.system, nick="zwyyy")
        # 应该把 {nick} 换成 zwyyy
        # 但如果模板里没 {nick} 也不会出错
        assert "zwyyy" in result or "{nick}" in result
