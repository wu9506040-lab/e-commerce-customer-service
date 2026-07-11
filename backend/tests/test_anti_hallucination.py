"""
P0-J：商品不存在反幻觉测试

防"ZP2 续航怎么样"这类诱导 LLM 编造参数的场景：
- 商品 DB 无命中 + KB 也无命中 → 不调 LLM，直接返兜底
- 商品 DB 无命中 + KB 有命中 → 仍可走 LLM，让 KB 兜底回答
- 商品 DB 有命中 → 正常走 LLM
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest


# 让模块能找到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _drain(gen):
    """把 generator 抽干，返回 (events, llm_called)"""
    events = []
    llm_called = False
    for event_type, data in gen:
        events.append((event_type, data))
        if event_type == "token" and isinstance(data, str) and len(data) > 50:
            llm_called = True  # 大段 token 才算真正调了 LLM
    return events, llm_called


def test_product_not_found_no_llm():
    """场景 1：商品 DB 空 + KB 空 → 不调 LLM，返兜底"""
    with patch("app.services.synthesizer.ProductTool") as pt_mock, \
         patch("app.services.synthesizer.PolicyService") as ps_mock:
        # 商品全空
        pt_mock.get_by_sku.return_value = None
        pt_mock.search_by_keyword.return_value = []
        # KB 全空
        ps_mock.search_policy.return_value = []

        from app.services.synthesizer import Synthesizer
        events, llm_called = _drain(Synthesizer._handle_product(
            query="ZP2 续航怎么样",  # 不存在的商品
            intent_result={"intent": "product_query", "entities": {"sku": "ZP2"}, "confidence": 0.9, "method": "rule"},
            history=[],
        ))

    # 应该有 meta + token（兜底文本）+ done
    event_types = [e[0] for e in events]
    assert "meta" in event_types, "应有 meta 事件"
    assert "token" in event_types, "应有 token 事件"
    assert "done" in event_types, "应有 done 事件"

    # 兜底文本应明确说"未找到"
    tokens = [d for t, d in events if t == "token"]
    full_text = "".join(tokens)
    assert "未找到" in full_text or "暂无" in full_text, \
        f"兜底文本应明确告知无资料, 实际: {full_text}"

    # meta 中 products_found=0 + kb_hits=0
    meta_data = [d for t, d in events if t == "meta"][0]
    assert meta_data["products_found"] == 0
    assert meta_data["kb_hits"] == 0

    print(f"PASS: 商品/KB 双空 → 兜底文本='{full_text[:40]}...'")


def test_product_not_found_but_kb_hit_goes_llm():
    """场景 2：商品 DB 空 + KB 命中 → 仍走 LLM（KB 兜底）"""
    fake_llm_chunks = ["ZP2", "是一款", "测试商品", "。"]

    with patch("app.services.synthesizer.ProductTool") as pt_mock, \
         patch("app.services.synthesizer.PolicyService") as ps_mock, \
         patch("app.services.synthesizer.get_llm_provider") as provider_mock:
        pt_mock.get_by_sku.return_value = None
        pt_mock.search_by_keyword.return_value = []
        # KB 命中 1 条
        ps_mock.search_policy.return_value = [
            {"text": "ZP2 是测试商品，续航 10 小时。", "source": "products.json", "score": 0.85}
        ]
        provider_mock.return_value.stream_chat.return_value = iter(fake_llm_chunks)

        from app.services.synthesizer import Synthesizer
        events, _ = _drain(Synthesizer._handle_product(
            query="ZP2 续航怎么样",
            intent_result={"intent": "product_query", "entities": {"sku": "ZP2"}, "confidence": 0.9, "method": "rule"},
            history=[],
        ))

    # 应该调 LLM（KB 有命中可以兜底回答）
    assert provider_mock.called, "KB 命中时应调 LLM 用 KB 内容回答"
    # meta 应有 kb_hits=1
    meta_data = [d for t, d in events if t == "meta"][0]
    assert meta_data["kb_hits"] == 1, f"meta 应显示 kb_hits=1, 实际={meta_data['kb_hits']}"

    print("PASS: 商品空 + KB 命中 → 走 LLM（KB 兜底）")


def test_product_found_normal():
    """场景 3：商品 DB 有命中 → 正常走 LLM"""
    fake_product = {
        "sku": "SKU001", "name": "ZP1 旗舰手机", "price": 2999, "stock": 50,
        "attributes": {"color": ["黑色", "白色"]},
    }
    fake_llm_chunks = ["ZP1", "售价", "2999元", "。"]

    with patch("app.services.synthesizer.ProductTool") as pt_mock, \
         patch("app.services.synthesizer.PolicyService") as ps_mock, \
         patch("app.services.synthesizer.get_llm_provider") as provider_mock:
        pt_mock.get_by_sku.return_value = fake_product
        pt_mock.search_by_keyword.return_value = [fake_product]
        ps_mock.search_policy.return_value = []
        provider_mock.return_value.stream_chat.return_value = iter(fake_llm_chunks)

        from app.services.synthesizer import Synthesizer
        events, _ = _drain(Synthesizer._handle_product(
            query="ZP1 多少钱",
            intent_result={"intent": "product_query", "entities": {"sku": "SKU001"}, "confidence": 0.9, "method": "rule"},
            history=[],
        ))

    assert provider_mock.called, "商品命中时应调 LLM"
    meta_data = [d for t, d in events if t == "meta"][0]
    assert meta_data["products_found"] == 1

    # meta 中 contexts 应包含商品信息
    assert len(meta_data["contexts"]) >= 1, "P0-H: meta 应暴露商品 context"
    assert meta_data["contexts"][0]["type"] == "product"

    print("PASS: 商品命中 → 正常 LLM 路径 + meta 暴露 product context")


if __name__ == "__main__":
    # 配置环境变量避免 config 校验报错
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    test_product_not_found_no_llm()
    test_product_not_found_but_kb_hit_goes_llm()
    test_product_found_normal()
    print("\nALL 3 SCENARIOS PASSED")