"""
C3 Agent FC 评测 harness - 单元测试 + 回归门禁

覆盖 scripts/eval_agent_fc.py 的核心逻辑：
- load_eval_set：格式校验
- 4 类指标计算：tool_selection_accuracy / tool_round_efficiency /
  answer_keyword_match / hallucination_free
- mini_judge：mock 模式 fallback 规则
- evaluate_case_mock：端到端（单工具 / 多工具 / direct 无工具）
- summarize：汇总统计

策略：mock LLM（side_effect 按 expected_tools 顺序返 tool_calls）+ mock dispatch，
不依赖真实服务；CI 可跑。评测集为 local artifact（.gitignore data/），
故测试用例全部 inline 构造 / tmp_path 落盘，不依赖 data/eval_agent_set.json。

依据：C3.1 commit「feat(rag-eval): 新增 Agent FC 决策质量评测 harness」配套测试。
"""
import os
import sys
import json
from pathlib import Path

import pytest

# pytest 不走 __main__，env 必须在模块顶部 setdefault
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")

# path 处理：tests/ 在 backend/tests/，要能 import app.* 和 scripts.*
TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent  # backend/
PROJECT_ROOT = BACKEND_DIR.parent  # 智能客服/
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.eval_agent_fc import (  # noqa: E402
    load_eval_set,
    compute_tool_selection_accuracy,
    compute_tool_round_efficiency,
    compute_answer_keyword_match,
    compute_hallucination_free,
    mini_judge,
    evaluate_case_mock,
    summarize,
)


# =============================================================
# 工具函数
# =============================================================
def _make_case(**overrides) -> dict:
    """构造一条最小 mock 评测 case，可覆盖字段。"""
    base = {
        "query": "订单 SO001 到哪了",
        "expected_tools": [{"name": "lookup_order", "arguments_contains": {"order_no": "SO001"}}],
        "expected_answer_keywords": ["订单", "状态"],
        "sensitive_keywords": [],
        "category": "order_query",
        "expected_rounds": 1,
        "user_id": 1,
        "note": "test",
    }
    base.update(overrides)
    return base


# =============================================================
# 1. load_eval_set 校验
# =============================================================
def test_load_eval_set_success(tmp_path):
    p = tmp_path / "eval.json"
    p.write_text(json.dumps([_make_case()], ensure_ascii=False), encoding="utf-8")
    result = load_eval_set(p)
    assert len(result) == 1
    assert result[0]["query"] == "订单 SO001 到哪了"


def test_load_eval_set_missing_required_field(tmp_path):
    p = tmp_path / "eval.json"
    bad = _make_case()
    del bad["expected_tools"]
    p.write_text(json.dumps([bad], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="缺字段"):
        load_eval_set(p)


def test_load_eval_set_expected_tools_not_list(tmp_path):
    p = tmp_path / "eval.json"
    bad = _make_case(expected_tools="not-a-list")
    p.write_text(json.dumps([bad], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="expected_tools 必须是 list"):
        load_eval_set(p)


def test_load_eval_set_wrong_top_level_format(tmp_path):
    p = tmp_path / "eval.json"
    p.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="期望 list"):
        load_eval_set(p)


def test_load_eval_set_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_eval_set(Path("/nonexistent/eval.json"))


# =============================================================
# 2. 4 类指标计算
# =============================================================
def test_tool_selection_accuracy_full_match():
    expected = [{"name": "lookup_order"}, {"name": "search_policy"}]
    assert compute_tool_selection_accuracy(["lookup_order", "search_policy"], expected) == 1.0


def test_tool_selection_accuracy_half_match():
    expected = [{"name": "lookup_order"}, {"name": "search_policy"}]
    assert compute_tool_selection_accuracy(["lookup_order"], expected) == 0.5


def test_tool_selection_accuracy_empty_expected_is_full():
    # direct case：期望不调工具 = 满分
    assert compute_tool_selection_accuracy([], []) == 1.0


def test_tool_round_efficiency_within_budget():
    assert compute_tool_round_efficiency(1, 1) == 1.0
    assert compute_tool_round_efficiency(1, 2) == 1.0  # 少于预算也算高效


def test_tool_round_efficiency_over_budget_penalized():
    # 实际 4 轮，预期 2 轮 → 2/4 = 0.5
    assert compute_tool_round_efficiency(4, 2) == 0.5


def test_answer_keyword_match():
    assert compute_answer_keyword_match("您的订单状态是已发货", ["订单", "状态"]) == 1.0
    assert compute_answer_keyword_match("您的订单已发货", ["订单", "状态"]) == 0.5
    assert compute_answer_keyword_match("", ["订单"]) == 0.0
    assert compute_answer_keyword_match("任意答案", []) == 1.0  # 无期望词 = 满分


def test_hallucination_free():
    assert compute_hallucination_free("订单已发货", ["直接退款", "全额赔偿"]) is True
    assert compute_hallucination_free("我给你直接退款", ["直接退款"]) is False
    assert compute_hallucination_free("任意答案", []) is True  # 无敏感词 = 通过
    assert compute_hallucination_free("", ["直接退款"]) is True  # 空答案不算幻觉


# =============================================================
# 3. mini_judge（mock 模式 fallback）
# =============================================================
def test_mini_judge_mock_fallback_pass():
    os.environ["EVAL_AGENT_FC_MOCK"] = "1"
    r = mini_judge("这是一个正常答案", ["答案"], [])
    assert r["judge"] == 1


def test_mini_judge_mock_fallback_empty_fail():
    os.environ["EVAL_AGENT_FC_MOCK"] = "1"
    r = mini_judge("", ["答案"], [])
    assert r["judge"] == 0


def test_mini_judge_mock_fallback_sensitive_fail():
    os.environ["EVAL_AGENT_FC_MOCK"] = "1"
    r = mini_judge("我给你全额赔偿", [], ["全额赔偿"])
    assert r["judge"] == 0


# =============================================================
# 4. evaluate_case_mock 端到端
# =============================================================
def test_evaluate_case_mock_single_tool():
    case = _make_case()
    r = evaluate_case_mock(case)
    assert not r.get("skipped"), f"case skipped: {r.get('reason')}"
    assert r["actual_tools"] == ["lookup_order"]
    assert r["metrics"]["tool_selection_accuracy"] == 1.0
    assert r["metrics"]["hallucination_free"] is True


def test_evaluate_case_mock_multi_tool():
    case = _make_case(
        query="订单 SO002 坏了能退吗",
        expected_tools=[
            {"name": "lookup_order", "arguments_contains": {"order_no": "SO002"}},
            {"name": "search_policy", "arguments_contains": {"keyword": "退货"}},
        ],
        expected_answer_keywords=["订单", "退货"],
        expected_rounds=2,
        category="mixed",
    )
    r = evaluate_case_mock(case)
    assert not r.get("skipped"), f"case skipped: {r.get('reason')}"
    assert r["actual_tools"] == ["lookup_order", "search_policy"]
    assert r["metrics"]["tool_selection_accuracy"] == 1.0
    assert r["actual_rounds"] == 2


def test_evaluate_case_mock_direct_no_tool():
    case = _make_case(
        query="你好",
        expected_tools=[],
        expected_answer_keywords=["您好"],
        expected_rounds=0,
        category="direct",
    )
    r = evaluate_case_mock(case)
    assert not r.get("skipped"), f"case skipped: {r.get('reason')}"
    assert r["actual_tools"] == []
    assert r["metrics"]["tool_selection_accuracy"] == 1.0


# =============================================================
# 5. summarize 汇总
# =============================================================
def test_summarize_basic():
    results = [
        evaluate_case_mock(_make_case()),
        evaluate_case_mock(_make_case(query="订单 SO003 状态", category="order_query")),
    ]
    summary = summarize(results)
    assert summary["total"] == 2
    assert summary["valid"] == 2
    assert summary["errors"] == 0
    assert summary["avg_metrics"]["tool_selection_accuracy"] == 1.0
    assert "order_query" in summary["by_category"]


def test_summarize_empty():
    assert summarize([]) == {}


def test_summarize_all_errors():
    results = [{"category": "x", "query": "q", "error": "boom", "mode": "mock"}]
    summary = summarize(results)
    assert summary["valid"] == 0
    assert summary["errors"] == 1
