"""
V21 M3-4 测试：连发合并批"闲聊让路"多意图分类

背景：买家连发"在吗 / 这个多大 / 多少钱 / 什么时候发货"——
旧逻辑 `for cand: if hit: return hit` 首条闲聊短路整批，实质问题全漏
（问候变体"老板在吗"不在 reply_scheduler 剥离白名单，仍会进分类器）。
V18 拼装误判事故（2026-10-17）修的是"合并包装文本致 LLM 误判"方向；
本文件覆盖的是残余缺口：合并批内闲聊命中让路 + 实质意图按序收集。

覆盖：
1. 合并批 闲聊开头 + 实质问题 → 实质意图胜出（不让闲聊短路整批）
2. 合并批 多个实质意图 → rule_multi 多意图、primary=首个实质意图、顺序=消息顺序
3. 合并批 全部纯问候 → 仍判 chitchat（整批闲聊，行为不变）
4. 合并批 全部未命中规则 → 走 LLM 兜底（mock，喂干净拼接原文）
5. 单条消息路径不受影响："在吗" 单条仍 chitchat 短路
"""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture(autouse=True)
def _isolate_intent_state_after():
    from app.services import intent_service
    from app.services.config_loader import reset_config_loader

    saved_enabled = intent_service._MULTI_INTENT_ENABLED
    saved_top_k = intent_service.TOP_K
    yield
    intent_service._MULTI_INTENT_ENABLED = saved_enabled
    intent_service.TOP_K = saved_top_k
    reset_config_loader()


def _merged(*msgs: str) -> str:
    """构造 reply_scheduler._build_query 的合并包装格式。"""
    lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(msgs))
    return (
        "用户连续发了以下几条消息：\n"
        f"{lines}\n"
        "请你综合回答这几个问题，一条回复里覆盖所有问题。"
    )


# =============================================================
# 1. 闲聊让路：首条闲聊不再短路整批
# =============================================================
class TestChitchatYield:
    def test_greeting_first_does_not_swallow_batch(self):
        """在吗 + 容量 + 发货 → 实质意图胜出，非 chitchat。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify(_merged(
            "在吗", "这个充电宝多大容量", "什么时候发货",
        ))
        assert result["primary"] != "chitchat"
        assert result["method"] == "rule_multi"
        names = [i["intent"] for i in result["intents"]]
        assert names == ["product_query", "policy_query"]  # 顺序 = 消息顺序

    def test_greeting_variant_not_stripped_by_scheduler(self):
        """问候变体"老板在吗"（scheduler 剥不掉）+ 实质问题 → 仍让路。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify(_merged(
            "老板在吗", "这个充电宝多大容量",
        ))
        assert result["primary"] == "product_query"
        assert result["method"] == "rule_multi"

    def test_chitchat_rule_message_yields(self):
        """规则层 chitchat（^你好 前缀）也走让路，不短路整批。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify(_merged(
            "你好", "这个充电宝多大容量",
        ))
        assert result["primary"] == "product_query"


# =============================================================
# 2. 多意图收集：primary=首个实质意图，intents 全量保留
# =============================================================
class TestMultiIntentCollect:
    def test_three_substantive_intents_all_kept(self):
        """3 个实质意图全部保留（不受 TOP_K=2 截断——合并批不受 top-K 限制）。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify(_merged(
            "在吗", "这个充电宝多大容量", "运费多少", "我的物流到哪了",
        ))
        names = [i["intent"] for i in result["intents"]]
        assert names == ["product_query", "policy_query", "order_query"]
        assert result["primary"] == "product_query"

    def test_duplicate_intents_deduped(self):
        from app.services.intent_service import IntentService

        result = IntentService.classify(_merged(
            "多少钱", "这个什么价格",
        ))
        names = [i["intent"] for i in result["intents"]]
        assert names == ["product_query"]
        # 兼容别名（V12 契约）
        assert result["intent"] == "product_query"

    def test_cross_message_rule_on_joined_text(self):
        """跨消息规则（"怎么" + "退款" 分两条发）仍命中。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify(_merged(
            "在吗", "我想问下这个", "怎么退款",
        ))
        names = [i["intent"] for i in result["intents"]]
        assert "policy_query" in names


# =============================================================
# 3. 整批纯问候 / 全未命中：行为不变
# =============================================================
class TestUnchangedPaths:
    def test_all_greeting_batch_is_chitchat(self):
        from app.services.intent_service import IntentService

        result = IntentService.classify(_merged("在吗", "你好"))
        assert result["primary"] == "chitchat"
        assert result["method"] == "rule_short"

    def test_all_miss_batch_goes_llm_with_clean_text(self):
        """全未命中 → LLM 兜底，且喂干净拼接原文（不看包装头尾）。"""
        from app.services.intent_service import IntentService

        with patch("app.services.intent_service.get_llm_provider") as mock_llm:
            mock_llm.return_value.chat.return_value = {
                "reply": '{"intents":[{"intent":"order_query","confidence":0.8}]}'
            }
            result = IntentService.classify(_merged("帮我看看那个东西", "就是说呢这个"))
            # LLM 收到拼接原文（不含包装头尾）
            fed = mock_llm.return_value.chat.call_args.kwargs["messages"][0]["content"]
            assert "连续发" not in fed
            assert "帮我看看那个东西" in fed and "就是说呢这个" in fed
        assert result["primary"] == "order_query"

    def test_single_message_unchanged(self):
        """单条路径不受影响：单条"在吗"仍 chitchat 短路。"""
        from app.services.intent_service import IntentService

        assert IntentService.classify("在吗")["primary"] == "chitchat"
        assert IntentService.classify("老板在吗")["primary"] == "chitchat"
