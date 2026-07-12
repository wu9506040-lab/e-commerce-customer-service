"""
Sprint 3: chat.prompt_assembler._build_meta_contexts 单元测试

覆盖 _build_meta_contexts 的 4 个语义分支：
1. policy_docs：带 score → contexts type="policy" + scores 填
2. products：tool 查的 → type="product" + scores 用 0 占位
3. tool_result (order detail / orders list)：type="order" + scores 用 0 占位
4. 混合多类型：3 类同时存在 → 拼接顺序 (policy → product → order)

设计原则：
- 纯函数测试，无 I/O / 无 DB / 无 LLM 依赖
- 覆盖前端 meta.contexts[] 渲染所需的 source / text_preview / type 字段契约
- 文档化 contracts/api/chat.py SSE meta 事件的 payloads 结构

依据：docs/decisions/2026-07-12-sprint-3-synthesizer-split.md §6 + §10
"""
import os
import sys

# 让模块能找到 app 包（与项目其他测试一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================
# policy_docs 分支
# =============================================================

def test_meta_contexts_policy_with_score_includes_score():
    """policy_docs 带 score → contexts 含 type=policy，scores 浮点型"""
    from app.services.chat.prompt_assembler import _build_meta_contexts

    docs = [
        {"text": "7天无理由退货政策", "source": "policy_return", "score": 0.92},
        {"text": "运费险说明", "source": "policy_shipping", "score": 0.85},
    ]
    contexts, scores = _build_meta_contexts(policy_docs=docs)
    assert len(contexts) == 2
    assert all(c["type"] == "policy" for c in contexts)
    assert contexts[0]["source"] == "policy_return"
    assert contexts[1]["source"] == "policy_shipping"
    assert scores == [0.92, 0.85]


def test_meta_contexts_policy_score_int_accepted():
    """policy_docs score 是 int（不是 float）也应接受，转 float"""
    from app.services.chat.prompt_assembler import _build_meta_contexts

    docs = [{"text": "保修", "source": "policy_warranty", "score": 1}]  # int
    contexts, scores = _build_meta_contexts(policy_docs=docs)
    assert scores == [1.0]
    assert isinstance(scores[0], float)


def test_meta_contexts_policy_truncates_long_text_to_200():
    """policy_docs text_preview 截断 200 字符 + 加 ..."""
    from app.services.chat.prompt_assembler import _build_meta_contexts

    long_text = "A" * 500
    docs = [{"text": long_text, "source": "x", "score": 0.5}]
    contexts, _ = _build_meta_contexts(policy_docs=docs)
    # text_preview = text[:200] + "..."
    assert len(contexts[0]["text_preview"]) == 200 + 3
    assert contexts[0]["text_preview"].endswith("...")


# =============================================================
# products 分支
# =============================================================

def test_meta_contexts_products_uses_zero_placeholder_score():
    """products 无 cosine 分数 → scores 用 0 占位（前端可识别为 tool 数据）"""
    from app.services.chat.prompt_assembler import _build_meta_contexts

    products = [
        {"sku": "SKU001", "name": "ZP1 旗舰手机", "price": 2999, "stock": 50},
    ]
    contexts, scores = _build_meta_contexts(products=products)
    assert len(contexts) == 1
    assert contexts[0]["type"] == "product"
    assert contexts[0]["source"] == "product:SKU001"
    assert "ZP1" in contexts[0]["text_preview"]
    assert scores == [0.0]


# =============================================================
# tool_result 分支（order_query）
# =============================================================

def test_meta_contexts_tool_result_order_detail():
    """tool_result 单订单详情：type=order，source=order:ORDxxx"""
    from app.services.chat.prompt_assembler import _build_meta_contexts

    tool_result = {
        "order": {"order_no": "ORD001", "status": "shipped", "total_amount": 2999},
        "items": [{"product_name": "ZP1", "qty": 1}],
        "logistics": {"status": "运输中"},
    }
    contexts, scores = _build_meta_contexts(tool_result=tool_result)
    assert len(contexts) == 1
    assert contexts[0]["type"] == "order"
    assert contexts[0]["source"] == "order:ORD001"
    assert "shipped" in contexts[0]["text_preview"]
    assert scores == [0.0]


def test_meta_contexts_tool_result_order_list():
    """tool_result 订单列表：每个订单一条 context"""
    from app.services.chat.prompt_assembler import _build_meta_contexts

    tool_result = {
        "orders": [
            {"order_no": "ORD001", "status": "delivered", "total_amount": 99.0},
            {"order_no": "ORD002", "status": "shipped", "total_amount": 199.0},
        ]
    }
    contexts, scores = _build_meta_contexts(tool_result=tool_result)
    assert len(contexts) == 2
    assert contexts[0]["source"] == "order:ORD001"
    assert contexts[1]["source"] == "order:ORD002"
    assert scores == [0.0]


# =============================================================
# 混合多类型
# =============================================================

def test_meta_contexts_mixed_all_three_types():
    """policy + products + tool_result 三类同时存在 → 拼接顺序（policy → product → order）"""
    from app.services.chat.prompt_assembler import _build_meta_contexts

    policy_docs = [{"text": "7天无理由", "source": "policy_return", "score": 0.9}]
    products = [{"sku": "SKU001", "name": "ZP1", "price": 2999, "stock": 50}]
    tool_result = {"order": {"order_no": "ORD001", "status": "shipped", "total_amount": 2999}}

    contexts, scores = _build_meta_contexts(
        policy_docs=policy_docs,
        products=products,
        tool_result=tool_result,
    )

    assert len(contexts) == 3
    # 顺序：先 policy → 再 product → 最后 tool_result
    assert [c["type"] for c in contexts] == ["policy", "product", "order"]
    # scores：policy 有分数；products/tool 用 0 占位
    assert scores == [0.9, 0.0, 0.0]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
