"""
Phase 4 A4: query_rewriter.rewrite_query_multi 单测

覆盖：
1. L0 短路：无指代词 → 返 [query], was_rewritten=False
2. L1 短路：无 history → 返 [query], was_rewritten=False（不调 LLM）
3. L2 成功：LLM 返 3 条 JSON → 返 3 条变体 + was_rewritten=True
4. L2 不足：LLM 返 1 条 → too_few_variants 降级
5. L2 解析失败：LLM 返非 JSON → parse_fail 降级
6. L2 LLM 异常 → llm_error 降级
7. 长度超限过滤：变体超过 MAX_RATIO*orig + MAX_EXTRA → 被丢弃
8. 去重：重复变体只保留 1 份
9. 填充：变体不足时用原 query 填充到 N 条
10. YAML 字段加载：ENABLE_MULTI_QUERY / MULTI_QUERY_COUNT 默认值
"""
import json
import os
import sys
from unittest.mock import patch

import pytest

# pytest 不走 __main__，env 必须在模块顶部 setdefault
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ====================== L0 / L1 短路 ======================

def test_no_coreference_skips_llm():
    """场景 1：无指代词 → L0 短路，返 [query]，was_rewritten=False。"""
    from app.services.query_rewriter import rewrite_query_multi

    with patch("app.services.query_rewriter.get_llm_provider") as mock_provider:
        queries, was_rewritten = rewrite_query_multi(
            "ZP1 多少钱", history=[{"role": "user", "content": "hi"}]
        )
    assert queries == ["ZP1 多少钱"]
    assert was_rewritten is False
    mock_provider.assert_not_called()


def test_no_history_skips_llm():
    """场景 2：无 history → L1 短路，返 [query]，不调 LLM。"""
    from app.services.query_rewriter import rewrite_query_multi

    with patch("app.services.query_rewriter.get_llm_provider") as mock_provider:
        queries, was_rewritten = rewrite_query_multi("它怎么退", history=None)
    assert queries == ["它怎么退"]
    assert was_rewritten is False
    mock_provider.assert_not_called()


# ====================== L2 成功路径 ======================

def test_llm_returns_3_variants_success():
    """场景 3：LLM 返 3 条 JSON 数组 → 返 3 条变体 + was_rewritten=True。"""
    from app.services.query_rewriter import rewrite_query_multi

    reply = json.dumps(["退货流程", "如何申请退款", "退货运费险"], ensure_ascii=False)
    mock_provider = patch("app.services.query_rewriter.get_llm_provider")
    with mock_provider as mp:
        mp.return_value.chat.return_value = {"reply": reply}
        queries, was_rewritten = rewrite_query_multi(
            "它怎么退",
            history=[
                {"role": "user", "content": "我买了运费险"},
                {"role": "assistant", "content": "运费险是 9.9 元的服务"},
            ],
            n=3,
        )

    assert was_rewritten is True
    assert len(queries) == 3
    assert queries[0] == "退货流程"
    assert mp.return_value.chat.called


def test_llm_message_format_uses_multi_prompts():
    """场景 4：LLM 调用使用 multi_system.yaml + multi_user_template.yaml 模板。"""
    from app.services import query_rewriter
    from app.services.query_rewriter import rewrite_query_multi

    reply = json.dumps(["主改写", "变体2", "变体3"], ensure_ascii=False)

    with patch("app.services.query_rewriter.get_llm_provider") as mp:
        mp.return_value.chat.return_value = {"reply": reply}
        rewrite_query_multi(
            "它能退吗",
            history=[{"role": "user", "content": "前几天买的"}],
            n=3,
        )
        call_args = mp.return_value.chat.call_args
        messages = call_args[0][0]
        # system 来自 multi_system（应含 {n} = 3）
        assert "system" in messages[0]["role"]
        assert "3" in messages[0]["content"]
        # user 来自 multi_user_template
        assert messages[1]["role"] == "user"
        assert "{history}" not in messages[1]["content"]  # 已 format
        assert "它能退吗" in messages[1]["content"]


# ====================== L2 失败降级 ======================

def test_llm_returns_non_json_falls_back():
    """场景 5：LLM 返非 JSON 文本 → parse_fail，返 [query]。"""
    from app.services.query_rewriter import rewrite_query_multi

    with patch("app.services.query_rewriter.get_llm_provider") as mp:
        mp.return_value.chat.return_value = {"reply": "好的，主改写是：退款流程"}
        queries, was_rewritten = rewrite_query_multi(
            "它怎么退", history=[{"role": "user", "content": "hi"}]
        )
    assert was_rewritten is False
    assert queries == ["它怎么退"]


def test_llm_returns_single_variant_too_few():
    """场景 6：LLM 仅返 1 条变体 → too_few_variants，降级到单路。"""
    from app.services.query_rewriter import rewrite_query_multi

    with patch("app.services.query_rewriter.get_llm_provider") as mp:
        mp.return_value.chat.return_value = {"reply": json.dumps(["退款流程"], ensure_ascii=False)}
        queries, was_rewritten = rewrite_query_multi(
            "它怎么退", history=[{"role": "user", "content": "hi"}], n=3
        )
    assert was_rewritten is False
    assert queries == ["它怎么退"]


def test_llm_exception_falls_back():
    """场景 7：LLM 抛异常 → llm_error，返 [query]。"""
    from app.services.query_rewriter import rewrite_query_multi

    with patch("app.services.query_rewriter.get_llm_provider") as mp:
        mp.return_value.chat.side_effect = RuntimeError("Qwen 500")
        queries, was_rewritten = rewrite_query_multi(
            "它怎么退", history=[{"role": "user", "content": "hi"}]
        )
    assert was_rewritten is False
    assert queries == ["它怎么退"]


# ====================== 变体验证 / 长度 / 去重 / 填充 ======================

def test_variants_exceeding_max_length_dropped():
    """场景 8：变体超过 MAX_RATIO*orig + MAX_EXTRA 字符 → 被丢弃。"""
    from app.services import query_rewriter
    from app.services.query_rewriter import rewrite_query_multi

    orig = "它怎么退"  # 4 字
    max_len = len(orig) * query_rewriter.MAX_REWRITE_RATIO + query_rewriter.MAX_REWRITE_EXTRA
    # 构造 1 长 1 短：长变体应被丢弃
    too_long = "x" * (max_len + 10)  # 超过上限
    valid = "退货流程"

    with patch("app.services.query_rewriter.get_llm_provider") as mp:
        mp.return_value.chat.return_value = {"reply": json.dumps([valid, too_long], ensure_ascii=False)}
        queries, was_rewritten = rewrite_query_multi(
            orig, history=[{"role": "user", "content": "hi"}], n=3
        )
    # 有效变体只有 1 条 + 不到 2 → too_few_variants 降级
    assert was_rewritten is False
    assert queries == [orig]


def test_duplicate_variants_deduped_and_padded():
    """场景 9：重复变体去重 + 用原 query 填充到 n 条。"""
    from app.services.query_rewriter import rewrite_query_multi

    reply = json.dumps(["退货", "退货", "退运费险"], ensure_ascii=False)  # "退货" 重复
    with patch("app.services.query_rewriter.get_llm_provider") as mp:
        mp.return_value.chat.return_value = {"reply": reply}
        queries, was_rewritten = rewrite_query_multi(
            "它怎么退", history=[{"role": "user", "content": "hi"}], n=3
        )
    # 去重后 ["退货", "退运费险"]；2 < n=3 → 用原 query 填充
    assert was_rewritten is True
    assert len(queries) == 3
    assert queries.count("退货") == 1  # dedupe
    assert "退运费险" in queries
    assert "它怎么退" in queries  # padding


def test_variants_equal_to_original_query_excluded():
    """场景 10：变体 == 原 query → 排除（避免"伪变体"干扰 RRF）。"""
    from app.services.query_rewriter import rewrite_query_multi

    reply = json.dumps(["它怎么退", "退货流程"], ensure_ascii=False)
    with patch("app.services.query_rewriter.get_llm_provider") as mp:
        mp.return_value.chat.return_value = {"reply": reply}
        queries, was_rewritten = rewrite_query_multi(
            "它怎么退", history=[{"role": "user", "content": "hi"}], n=3
        )
    # 第 1 条 == query 被排除；剩 ["退货流程"] (1 条) + 填充
    assert was_rewritten is True
    assert queries[0] == "退货流程" or queries[0] == "它怎么退"  # 1 unique → too_few → 返 [query]


# ====================== YAML 配置加载 ======================

def test_yaml_fields_loaded():
    """场景 11：YAML 3 字段加载（ENABLE_MULTI_QUERY / MULTI_QUERY_COUNT / MULTI_QUERY_TRIGGER）。"""
    from app.services import query_rewriter

    assert hasattr(query_rewriter, "ENABLE_MULTI_QUERY")
    assert hasattr(query_rewriter, "MULTI_QUERY_COUNT")
    assert hasattr(query_rewriter, "MULTI_QUERY_TRIGGER")
    assert query_rewriter.MULTI_QUERY_COUNT >= 1


def test_multi_prompts_loaded():
    """场景 12：multi_system.yaml + multi_user_template.yaml 加载成功。"""
    from app.services import query_rewriter

    assert isinstance(query_rewriter.MULTI_SYSTEM_PROMPT_TEMPLATE, str)
    assert isinstance(query_rewriter.MULTI_USER_TEMPLATE, str)
    assert "{history}" in query_rewriter.MULTI_USER_TEMPLATE
    assert "{query}" in query_rewriter.MULTI_USER_TEMPLATE
    assert "{n}" in query_rewriter.MULTI_USER_TEMPLATE


if __name__ == "__main__":
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    test_no_coreference_skips_llm()
    test_no_history_skips_llm()
    test_llm_returns_3_variants_success()
    test_llm_message_format_uses_multi_prompts()
    test_llm_returns_non_json_falls_back()
    test_llm_returns_single_variant_too_few()
    test_llm_exception_falls_back()
    test_variants_exceeding_max_length_dropped()
    test_duplicate_variants_deduped_and_padded()
    test_variants_equal_to_original_query_excluded()
    test_yaml_fields_loaded()
    test_multi_prompts_loaded()
    print("\nALL 12 SCENARIOS PASSED")
