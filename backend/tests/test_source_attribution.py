"""
P0-LLM 溯源：单测覆盖 prompt 硬约束 + 来源标签

防幻觉靠 3 层防护：
1. SYSTEM_PROMPT_BASE 硬约束（"严禁编造"、"强制溯源"）
2. 来源标签（tool/product/policy block 各加 [订单]/[退款]/[商品]/[知识库]/[1] 标签）
3. _build_chat_prompt 拼接逻辑正确（标签进入 LLM 上下文）

单测只测前 2 层（不调真实 LLM）。LLM 是否真的引用 [1] 由 ECS 端到端验证。
"""
import os
import sys
from unittest.mock import patch

import pytest

# 让模块能找到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_system_prompt_has_anti_hallucination_constraints():
    """测试 1：SYSTEM_PROMPT_BASE 含 4 条防幻觉硬约束"""
    from app.services.chat.prompt_assembler import SYSTEM_PROMPT_BASE
    required = [
        "严禁编造",         # 不允许自由发挥
        "引用标签",         # 强制溯源
        "[1][2]",          # 政策编号
        "[订单]",          # 订单标签
        "[退款]",          # 退款标签
        "[商品]",          # 商品标签
        "[知识库]",        # 知识库标签
        "我不知道",         # 无资料兜底
        "200 字以内",      # 长度限制
    ]
    for kw in required:
        assert kw in SYSTEM_PROMPT_BASE, f"SYSTEM_PROMPT_BASE 缺少硬约束: {kw!r}"
    print(f"PASS: SYSTEM_PROMPT_BASE 含 {len(required)} 条防幻觉硬约束")


def test_format_tool_result_order_query_has_tag():
    """测试 2：order_query 的 tool_result 加 [订单] 标签"""
    from app.services.chat.prompt_assembler import _format_tool_result

    # 场景 A: 订单列表
    out = _format_tool_result("order_query", {
        "orders": [{"order_no": "ORD001", "status": "shipped", "total_amount": 2999, "create_time": "2026-07-01"}]
    })
    assert "[订单]" in out, f"order 列表缺 [订单]: {out}"
    # 不能用 "用户当前没有订单" 这种无标签兜底以外的旧写法
    print(f"PASS: order 列表 → [订单]")

    # 场景 B: 订单详情
    out = _format_tool_result("order_query", {
        "order": {"order_no": "ORD001", "status": "paid", "total_amount": 99},
        "items": [{"product_name": "ZP1", "qty": 1, "subtotal": 99}],
        "logistics": {"status": "运输中", "last_location": "上海", "logistics_no": "SF123"},
    })
    assert out.count("[订单]") >= 2, f"order 详情应至少 2 个 [订单]: {out}"
    print(f"PASS: order 详情 → [订单] x{out.count('[订单]')}")

    # 场景 C: 空订单
    out = _format_tool_result("order_query", {"orders": []})
    assert "[订单]" in out and "没有订单" in out
    print(f"PASS: 空订单列表 → [订单] + 兜底文本")


def test_format_tool_result_refund_query_has_tag():
    """测试 3：refund_query 的 tool_result 加 [退款] 标签"""
    from app.services.chat.prompt_assembler import _format_tool_result
    out = _format_tool_result("refund_query", {
        "refundable": True, "reason": "7天无理由", "order_status": "delivered", "days_since_order": 3
    })
    assert "[退款]" in out
    assert "可退" in out
    print(f"PASS: refund_query → [退款] + 退款结论")

    # 不可退场景
    out = _format_tool_result("refund_query", {
        "refundable": False, "reason": "已退款", "order_status": "refunded", "days_since_order": 10
    })
    assert "[退款]" in out and "不可退" in out
    print(f"PASS: refund_query 不可退 → [退款] + 不可退")


def test_format_policy_docs_has_numbered_refs():
    """测试 4：policy docs 加 [1][2] 编号"""
    from app.services.chat.prompt_assembler import _format_policy_docs
    out = _format_policy_docs([
        {"text": "7天无理由退货政策", "source": "policy_return", "score": 0.9},
        {"text": "运费险说明", "source": "policy_shipping", "score": 0.8},
        {"text": "保修条款", "source": "policy_warranty", "score": 0.7},
    ])
    assert "[1]" in out and "[2]" in out and "[3]" in out
    # 编号必须按顺序
    assert out.index("[1]") < out.index("[2]") < out.index("[3]")
    print(f"PASS: policy docs → [1][2][3] 顺序编号")


def test_handle_product_empty_uses_safe_fallback():
    """测试 5：商品 + KB 双空时 _handle_product 走 _stream_simple（不调 LLM）

    这是 P0-J 已加的防护。溯源版本需要确认标签也加了 [商品]。
    """
    with patch("app.services.chat.orchestrator.ProductTool") as pt_mock, \
         patch("app.services.chat.stream_dispatcher.ProductTool") as pt_mock_stream, \
         patch("app.services.chat.orchestrator.PolicyService") as ps_mock, \
         patch("app.services.chat.stream_dispatcher.get_llm_provider") as provider_mock:
        pt_mock.get_by_sku.return_value = None
        pt_mock.search_by_keyword.return_value = []
        pt_mock_stream.search_by_keyword.return_value = []
        ps_mock.search_policy.return_value = []

        from app.services.chat.orchestrator import Synthesizer
        events = list(Synthesizer._handle_product(
            query="ZP99 续航怎么样",
            intent_result={"intent": "product_query", "entities": {"sku": "ZP99"}, "confidence": 0.9, "method": "rule"},
            history=[],
        ))

    # 必须没调 LLM
    assert not provider_mock.called, "商品+KB 双空时不应调 LLM"
    # meta + token (兜底) + done
    event_types = [e[0] for e in events]
    assert "meta" in event_types and "token" in event_types and "done" in event_types
    # 兜底文本中要提"暂无"/"无资料" + 不含具体规格
    tokens = "".join([d for t, d in events if t == "token"])
    assert "暂无" in tokens or "无" in tokens, f"兜底文本不明确: {tokens}"
    assert "骁龙" not in tokens and "mAh" not in tokens, f"兜底文本不应含具体规格: {tokens}"
    print(f"PASS: 商品+KB 双空 → 兜底文本（不调 LLM，无具体规格）")


def test_handle_product_with_real_data_uses_tagged_prompt():
    """测试 6：商品有命中时，_build_chat_prompt 里 product_block 加 [商品] 标签

    通过 mock qwen_stream_chat 抓 prompt，验证标签注入
    """
    fake_product = {
        "sku": "SKU001", "name": "ZP1 旗舰手机", "price": 2999, "stock": 50,
        "attributes": {"color": ["黑色"]},
    }
    captured_prompts = []

    def mock_qwen(messages, temperature, max_tokens):
        # 抓 user message（即 prompt）
        captured_prompts.append(messages[1]["content"])
        return iter(["ZP1 售价 2999 元"])

    with patch("app.services.chat.orchestrator.ProductTool") as pt_mock, \
         patch("app.services.chat.stream_dispatcher.ProductTool") as pt_mock_stream, \
         patch("app.services.chat.orchestrator.PolicyService") as ps_mock, \
         patch("app.services.chat.stream_dispatcher.get_llm_provider") as provider_mock:
        pt_mock.get_by_sku.return_value = fake_product
        pt_mock.search_by_keyword.return_value = [fake_product]
        pt_mock_stream.search_by_keyword.return_value = [fake_product]
        ps_mock.search_policy.return_value = []
        provider_mock.return_value.stream_chat.side_effect = mock_qwen

        from app.services.chat.orchestrator import Synthesizer
        list(Synthesizer._handle_product(
            query="ZP1 多少钱",
            intent_result={"intent": "product_query", "entities": {"sku": "SKU001"}, "confidence": 0.9, "method": "rule"},
            history=[],
        ))

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "[商品]" in prompt, f"商品 prompt 缺 [商品] 标签: {prompt[:300]}"
    print(f"PASS: 商品 prompt 含 [商品] 标签")


def test_handle_order_with_data_uses_tagged_prompt():
    """测试 7：order_query 走 LLM 时 prompt 含 [订单] 标签"""
    fake_order = {
        "order_no": "ORD001", "status": "shipped", "total_amount": 2999,
        "create_time": "2026-07-01T00:00:00",
    }
    fake_logistics = {"status": "运输中", "last_location": "上海", "logistics_no": "SF123"}
    captured_prompts = []

    def mock_qwen(messages, temperature, max_tokens):
        captured_prompts.append(messages[1]["content"])
        return iter(["订单在运输中"])

    with patch("app.services.chat.orchestrator.OrderService") as os_mock, \
         patch("app.services.chat.stream_dispatcher.get_llm_provider") as provider_mock:
        os_mock.get_order_detail.return_value = {
            "order": fake_order, "items": [{"product_name": "ZP1", "qty": 1, "subtotal": 2999}],
            "logistics": fake_logistics,
        }
        provider_mock.return_value.stream_chat.side_effect = mock_qwen

        from app.services.chat.orchestrator import Synthesizer
        list(Synthesizer._handle_order(
            query="ORD001 物流",
            user_id=7,
            intent_result={"intent": "order_query", "entities": {"order_no": "ORD001"}, "confidence": 0.9, "method": "rule"},
            order_no="ORD001",
            context_block="",
        ))

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "[订单]" in prompt, f"order prompt 缺 [订单] 标签: {prompt[:300]}"
    assert "ORD001" in prompt and "shipped" in prompt
    print(f"PASS: order prompt 含 [订单] 标签 + 完整事实")


def test_handle_policy_prompt_has_numbered_refs():
    """测试 8：policy_query 的 prompt 含 [1][2] 编号"""
    captured_prompts = []

    def mock_qwen(messages, temperature, max_tokens):
        captured_prompts.append(messages[1]["content"])
        return iter(["7天无理由政策..."])

    with patch("app.services.chat.orchestrator.PolicyService") as ps_mock, \
         patch("app.services.chat.stream_dispatcher.get_llm_provider") as provider_mock:
        ps_mock.search_policy.return_value = [
            {"text": "7天无理由退货政策", "source": "policy_return", "score": 0.92},
            {"text": "运费险说明", "source": "policy_shipping", "score": 0.85},
        ]
        provider_mock.return_value.stream_chat.side_effect = mock_qwen

        from app.services.chat.orchestrator import Synthesizer
        list(Synthesizer._handle_policy(
            query="7天无理由退货运费谁出",
            intent_result={"intent": "policy_query", "entities": {}, "confidence": 0.9, "method": "rule"},
            history=[],
        ))

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "[1]" in prompt and "[2]" in prompt, f"policy prompt 缺编号: {prompt[:300]}"
    print(f"PASS: policy prompt 含 [1][2] 编号")


if __name__ == "__main__":
    # 配置环境变量避免 config 校验报错
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    test_system_prompt_has_anti_hallucination_constraints()
    test_format_tool_result_order_query_has_tag()
    test_format_tool_result_refund_query_has_tag()
    test_format_policy_docs_has_numbered_refs()
    test_handle_product_empty_uses_safe_fallback()
    test_handle_product_with_real_data_uses_tagged_prompt()
    test_handle_order_with_data_uses_tagged_prompt()
    test_handle_policy_prompt_has_numbered_refs()
    print("\nALL 8 SCENARIOS PASSED")