"""
V12 多意图识别测试

覆盖：
1. classify() 返新结构（intents[] + primary + method + entities）
2. backward-compat 别名（intent/confidence = primary）
3. 规则命中只返 1 个 intent
4. ENABLE_MULTI_INTENT 灰度开关（true/false 两条路径）
5. LLM 多意图 JSON 解析（成功 / 失败 fallback）
6. LLM JSON 异常格式容错
7. top-K 截断
8. secondary_intent_block 注入 prompt_assembler
9. IntentResponse schema 接受新结构（intents + primary）
10. /api/intent/classify 端点返回新结构
"""
import os
import sys
from unittest.mock import patch

import pytest

# 让模块能找到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 触发工厂路径解析会 import app.core.config；提前 set 假值避免 _validate_jwt_secret 抛错
os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# =============================================================
# 隔离 fixture：每次测试结束后重置 config_loader 单例 + ENABLE_MULTI_INTENT 开关
# 背景：与 test_intent_config.py 同模式（autouse + post 隔离），
#       V12 多意图路径会改 _MULTI_INTENT_ENABLED / TOP_K 模块常量
# =============================================================
@pytest.fixture(autouse=True)
def _isolate_intent_state_after():
    from app.services import intent_service
    from app.services.config_loader import reset_config_loader

    # 备份模块级状态
    saved_enabled = intent_service._MULTI_INTENT_ENABLED
    saved_top_k = intent_service.TOP_K
    yield
    # 恢复
    intent_service._MULTI_INTENT_ENABLED = saved_enabled
    intent_service.TOP_K = saved_top_k
    reset_config_loader()


# =============================================================
# 1. classify() 新结构（intents[] + primary）
# =============================================================
class TestClassifyV12NewStructure:
    """V12：classify() 返新结构 + backward-compat 别名"""

    def test_classify_returns_intents_list(self):
        """classify() 必返 intents[] 列表（即使是单意图）。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我要退款")
        assert "intents" in result
        assert isinstance(result["intents"], list)
        assert len(result["intents"]) >= 1

    def test_classify_returns_primary(self):
        """classify() 必返 primary 字段。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我要退款")
        assert "primary" in result
        assert result["primary"] in ("order_query", "refund_query", "product_query", "policy_query")

    def test_classify_intents_sorted_by_confidence_desc(self):
        """intents[] 必按 confidence 降序。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("ZP1 现在有货吗")
        confidences = [i["confidence"] for i in result["intents"]]
        assert confidences == sorted(confidences, reverse=True)

    def test_classify_intent_alias_equals_primary(self):
        """backward-compat：result['intent'] == result['primary']。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我要退款")
        assert result["intent"] == result["primary"]

    def test_classify_confidence_alias_equals_primary_confidence(self):
        """backward-compat：result['confidence'] == primary 的 confidence。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我要退款")
        primary = result["primary"]
        primary_conf = next(i["confidence"] for i in result["intents"] if i["intent"] == primary)
        assert result["confidence"] == primary_conf

    def test_classify_method_preserved(self):
        """method 字段仍存在（rule / llm / default）。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我要退款")
        assert "method" in result
        assert result["method"] in ("rule", "llm", "default")

    def test_classify_entities_preserved(self):
        """entities 字段仍存在（含 order_no / sku / keywords）。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我的订单 ORD20260718001 到哪了")
        assert "entities" in result
        assert "order_no" in result["entities"]
        assert result["entities"]["order_no"] == "ORD20260718001"


# =============================================================
# 2. 规则命中只返 1 个 intent
# =============================================================
class TestRuleClassifySingleIntent:
    """V12：规则层天然单意图（intents 长度 = 1）"""

    def test_rule_match_refund(self):
        """规则命中 refund_query → intents 长度 = 1, primary = refund_query。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我要退款")
        assert len(result["intents"]) == 1
        assert result["intents"][0]["intent"] == "refund_query"
        assert result["intents"][0]["confidence"] == 1.0
        assert result["method"] == "rule"

    def test_rule_match_policy(self):
        """规则命中 policy_query → intents 长度 = 1。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("7 天无理由退货运费谁出")
        assert len(result["intents"]) == 1
        assert result["intents"][0]["intent"] == "policy_query"

    def test_rule_match_product(self):
        """规则命中 product_query → intents 长度 = 1。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("ZP1 现在有货吗")
        assert len(result["intents"]) == 1
        assert result["intents"][0]["intent"] == "product_query"

    def test_rule_match_order(self):
        """规则命中 order_query → intents 长度 = 1。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我的订单 ORD20260718001 到哪了")
        assert len(result["intents"]) == 1
        assert result["intents"][0]["intent"] == "order_query"


# =============================================================
# 3. 灰度开关 ENABLE_MULTI_INTENT
# =============================================================
class TestMultiIntentGraySwitch:
    """V12：ENABLE_MULTI_INTENT=false → LLM 走单意图路径（intents 仍 1 个）"""

    def test_multi_intent_off_uses_single_intent_llm(self):
        """ENABLE_MULTI_INTENT=False → LLM 路径走 _llm_classify（单意图）。"""
        from app.services import intent_service
        from app.services.intent_service import IntentService

        intent_service._MULTI_INTENT_ENABLED = False

        with patch.object(intent_service.IntentService, "_llm_classify") as mock_single, \
             patch.object(intent_service.IntentService, "_llm_classify_multi") as mock_multi:
            mock_single.return_value = {
                "intents": [{"intent": "product_query", "confidence": 0.9}],
                "primary": "product_query",
                "method": "llm",
            }
            result = IntentService.classify("这是一条不会命中规则的特殊 query")

            # 关键：单意图路径被调，多意图路径未被调
            assert mock_single.called
            assert not mock_multi.called
            assert result["method"] == "llm"
            assert len(result["intents"]) == 1

    def test_multi_intent_on_uses_multi_intent_llm(self):
        """ENABLE_MULTI_INTENT=True（默认）→ LLM 路径走 _llm_classify_multi。"""
        from app.services import intent_service
        from app.services.intent_service import IntentService

        intent_service._MULTI_INTENT_ENABLED = True  # 默认已开，显式置位

        with patch.object(intent_service.IntentService, "_llm_classify") as mock_single, \
             patch.object(intent_service.IntentService, "_llm_classify_multi") as mock_multi:
            mock_multi.return_value = {
                "intents": [
                    {"intent": "product_query", "confidence": 0.9},
                    {"intent": "policy_query", "confidence": 0.65},
                ],
                "primary": "product_query",
                "method": "llm",
            }
            result = IntentService.classify("这是一条不会命中规则的特殊 query")

            # 关键：多意图路径被调，单意图路径未被调
            assert mock_multi.called
            assert not mock_single.called
            assert result["primary"] == "product_query"
            assert len(result["intents"]) == 2


# =============================================================
# 4. LLM 多意图 JSON 解析
# =============================================================
class TestLLMMultiIntentParse:
    """V12：_llm_classify_multi() 解析 LLM 多意图 JSON 输出"""

    def test_multi_intent_parse_success(self):
        """LLM 返有效多意图 JSON → 解析成功，intents 长度 = 2。"""
        from app.services import intent_service
        from app.services.intent_service import IntentService

        llm_reply = {
            "reply": '{"intents": [{"intent": "product_query", "confidence": 0.9}, {"intent": "policy_query", "confidence": 0.65}]}'
        }
        with patch.object(intent_service.get_llm_provider(), "chat", return_value=llm_reply):
            result = IntentService._llm_classify_multi("电脑续航怎么样，能分期吗")
            assert len(result["intents"]) == 2
            assert result["intents"][0]["intent"] == "product_query"
            assert result["primary"] == "product_query"

    def test_multi_intent_parse_single(self):
        """LLM 返单意图（也走 multi 路径）→ 解析成功，intents 长度 = 1。"""
        from app.services import intent_service
        from app.services.intent_service import IntentService

        llm_reply = {"reply": '{"intents": [{"intent": "policy_query", "confidence": 0.9}]}'}
        with patch.object(intent_service.get_llm_provider(), "chat", return_value=llm_reply):
            result = IntentService._llm_classify_multi("保修多久")
            assert len(result["intents"]) == 1
            assert result["primary"] == "policy_query"

    def test_multi_intent_parse_with_markdown_fence(self):
        """LLM 返 ```json ... ``` 包装 → 宽松正则仍能解析。"""
        from app.services import intent_service
        from app.services.intent_service import IntentService

        llm_reply = {
            "reply": '```json\n{"intents": [{"intent": "refund_query", "confidence": 0.92}]}\n```'
        }
        with patch.object(intent_service.get_llm_provider(), "chat", return_value=llm_reply):
            result = IntentService._llm_classify_multi("我想退款")
            assert len(result["intents"]) == 1
            assert result["primary"] == "refund_query"

    def test_multi_intent_parse_failure_falls_back_to_default(self):
        """LLM 返非法 JSON → 走默认 policy_query fallback。"""
        from app.services import intent_service
        from app.services.intent_service import IntentService

        # rule_classify 不命中 → 进 LLM；LLM 返非法 JSON → 走 default fallback
        with patch.object(intent_service.get_llm_provider(), "chat", return_value={"reply": "无法解析的纯文本"}):
            result = IntentService.classify("这是一条不会命中规则的特殊 query xxxxxx")
            # fallback：default policy_query
            assert result["primary"] == "policy_query"
            assert result["method"] == "default"

    def test_multi_intent_parse_invalid_intent_filtered(self):
        """LLM 返非法 intent（如 unknown）→ 被过滤掉。"""
        from app.services import intent_service
        from app.services.intent_service import IntentService

        llm_reply = {
            "reply": '{"intents": [{"intent": "unknown_intent", "confidence": 0.9}, {"intent": "product_query", "confidence": 0.8}]}'
        }
        with patch.object(intent_service.get_llm_provider(), "chat", return_value=llm_reply):
            result = IntentService._llm_classify_multi("测试 query")
            # 非法 intent 被过滤，只保留 product_query
            assert len(result["intents"]) == 1
            assert result["intents"][0]["intent"] == "product_query"


# =============================================================
# 5. top-K 截断
# =============================================================
class TestTopKTruncation:
    """V12：TOP_K=2 截断生效"""

    def test_top_k_truncation(self):
        """LLM 返 3 个意图 → TOP_K=2 截断到 2 个。"""
        from app.services import intent_service
        from app.services.intent_service import IntentService

        intent_service.TOP_K = 2

        llm_reply = {
            "reply": '{"intents": ['
            '{"intent": "product_query", "confidence": 0.9}, '
            '{"intent": "policy_query", "confidence": 0.7}, '
            '{"intent": "order_query", "confidence": 0.5}'
            ']}'
        }
        with patch.object(intent_service.get_llm_provider(), "chat", return_value=llm_reply):
            result = IntentService._llm_classify_multi("测试 query")
            assert len(result["intents"]) == 2
            # 按 confidence 降序
            assert result["intents"][0]["intent"] == "product_query"
            assert result["intents"][1]["intent"] == "policy_query"

    def test_wrap_top_k_truncation(self):
        """_wrap_with_intent_alias 二次截断：LLM 返 3 个，TOP_K=2 → 仍截到 2。"""
        from app.services.intent_service import IntentService, _wrap_with_intent_alias

        result = _wrap_with_intent_alias({
            "intents": [
                {"intent": "a", "confidence": 0.9},
                {"intent": "b", "confidence": 0.7},
                {"intent": "c", "confidence": 0.5},
            ],
            "primary": "a",
            "method": "llm",
            "entities": {"order_no": None, "sku": None, "keywords": []},
        }, query="test")
        assert len(result["intents"]) == 2
        assert result["intents"][0]["intent"] == "a"
        assert result["intents"][1]["intent"] == "b"


# =============================================================
# 6. secondary_intent_block 注入 prompt_assembler
# =============================================================
class TestSecondaryIntentBlockInjection:
    """V12：secondary_intent_block 注入 _build_chat_prompt"""

    def test_secondary_block_appears_in_prompt(self):
        """secondary_intent_block 非空 → 出现在 prompt 中。"""
        from app.services.chat.prompt_assembler import _build_chat_prompt

        secondary_block = "用户问题可能还涉及以下意图，请在回答 primary 意图时一并简要覆盖：\n- policy_query（置信度 0.65）"

        prompt = _build_chat_prompt(
            intent="product_query",
            tool_block="",
            policy_block="",
            product_block="",
            history_block="",
            query="电脑续航怎么样",
            context_block="",
            secondary_intent_block=secondary_block,
        )

        assert "【用户可能的次要问题】(V12 多意图识别)" in prompt
        assert "policy_query" in prompt
        assert "0.65" in prompt

    def test_no_secondary_block_omits_section(self):
        """secondary_intent_block 为空 → 不出现该 section。"""
        from app.services.chat.prompt_assembler import _build_chat_prompt

        prompt = _build_chat_prompt(
            intent="product_query",
            tool_block="",
            policy_block="",
            product_block="",
            history_block="",
            query="电脑续航怎么样",
            context_block="",
        )

        assert "【用户可能的次要问题】" not in prompt

    def test_secondary_block_after_context_block(self):
        """secondary_intent_block 位于 context_block 之后、tool_block 之前。"""
        from app.services.chat.prompt_assembler import _build_chat_prompt

        prompt = _build_chat_prompt(
            intent="order_query",
            tool_block="[事实陈述] 订单数据",
            policy_block="",
            product_block="",
            history_block="",
            query="订单状态",
            context_block="【当前场景】M9.5 context",
            secondary_intent_block="【用户可能的次要问题】",
        )

        # 优先级：context > secondary > tool
        assert prompt.index("【当前场景】") < prompt.index("【用户可能的次要问题】")
        assert prompt.index("【用户可能的次要问题】") < prompt.index("【事实陈述】")


# =============================================================
# 7. IntentResponse schema 接受新结构
# =============================================================
class TestIntentResponseSchema:
    """V12：IntentResponse schema 接受 intents[] + primary（向后兼容 intent/confidence）"""

    def test_intent_response_accepts_new_structure(self):
        """IntentResponse 接受新结构。"""
        from app.schemas.intent import IntentResponse

        resp = IntentResponse(
            intents=[
                {"intent": "product_query", "confidence": 0.9},
                {"intent": "policy_query", "confidence": 0.65},
            ],
            primary="product_query",
            intent="product_query",  # 向后兼容别名
            confidence=0.9,
            method="llm",
            entities={"order_no": None, "sku": None, "keywords": []},
        )
        assert resp.primary == "product_query"
        assert resp.intent == "product_query"
        assert len(resp.intents) == 2

    def test_intent_response_intent_alias_serialization(self):
        """序列化时 intent 字段 = primary（前端读 .intent 仍可用）。"""
        from app.schemas.intent import IntentResponse

        resp = IntentResponse(
            intents=[{"intent": "refund_query", "confidence": 1.0}],
            primary="refund_query",
            intent="refund_query",
            confidence=1.0,
            method="rule",
        )
        serialized = resp.model_dump()
        assert serialized["intent"] == "refund_query"
        assert serialized["primary"] == "refund_query"
        assert serialized["confidence"] == 1.0
