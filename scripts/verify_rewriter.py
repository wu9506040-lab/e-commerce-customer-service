"""
M12 query_rewriter 验证脚本（unit-test 版，不启服务）

覆盖 7 个 case：
1. 含指代词 + 有 history → 应改写，结果含 history 里的实体
2. 无指代词 → 应跳过（不改写）
3. 含指代词 + 无 history → 应跳过
4. 复杂指代（这个和那个）→ 应改写
5. 长 query 无指代 → 应跳过
6. LLM 异常 → 降级返原 query
7. 空 query → 直接返空

用法：
    cd backend && python ../scripts/verify_rewriter.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

# 让脚本能找到 backend/app/
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.services.query_rewriter import rewrite_query  # noqa: E402

# ASCII 标记（Windows GBK 终端兼容）
PASS = "[PASS]"
FAIL = "[FAIL]"


def _check(condition: bool, name: str, detail: str = "") -> bool:
    mark = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {mark} {name}{suffix}")
    return condition


# =============================================================
# Case 实现
# =============================================================
def case_1_basic_coreference():
    """含指代词 + 有 history → 应改写"""
    print("\n[Case 1] 含指代 + 有 history")
    history = [
        {"role": "user", "content": "想看看 iPhone 15 Pro"},
        {"role": "assistant", "content": "iPhone 15 Pro 当前售价 8999 元"},
    ]
    rewritten, was_rewritten = rewrite_query("它能便宜点吗", history)
    ok1 = _check(was_rewritten, "was_rewritten=True", f"result='{rewritten}'")
    ok2 = _check(
        "iphone" in rewritten.lower() or "15 pro" in rewritten.lower(),
        "改写结果含 history 实体",
        f"result='{rewritten}'",
    )
    return ok1 and ok2


def case_2_no_coreference():
    """无指代词 → 应跳过"""
    print("\n[Case 2] 无指代词")
    history = [
        {"role": "user", "content": "iPhone 15 Pro"},
        {"role": "assistant", "content": "好的"},
    ]
    rewritten, was_rewritten = rewrite_query("续航怎么样", history)
    return _check(
        not was_rewritten and rewritten == "续航怎么样",
        "无指代词 → 不改写",
        f"result='{rewritten}'",
    )


def case_3_coref_no_history():
    """含指代词 + 无 history → 应跳过"""
    print("\n[Case 3] 含指代 + 无 history")
    rewritten, was_rewritten = rewrite_query("它能便宜吗", None)
    return _check(
        not was_rewritten and rewritten == "它能便宜吗",
        "无 history → 不改写",
        f"result='{rewritten}'",
    )


def case_4_complex_coref():
    """复杂指代（这个和那个）→ 应改写"""
    print("\n[Case 4] 复杂指代")
    history = [
        {"role": "user", "content": "iPhone 15 Pro 和华为 Mate 60 哪个好"},
        {"role": "assistant", "content": "两款都不错，看您需求"},
    ]
    rewritten, was_rewritten = rewrite_query("这个和那个哪个好", history)
    return _check(
        was_rewritten,
        "复杂指代 → 改写",
        f"result='{rewritten}'",
    )


def case_5_long_query_no_coref():
    """长 query 无指代 → 应跳过"""
    print("\n[Case 5] 长 query 无指代")
    history = [
        {"role": "user", "content": "想看 iPhone"},
        {"role": "assistant", "content": "好的"},
    ]
    long_q = "请问 iPhone 15 Pro 的电池续航时间和充电速度如何"
    rewritten, was_rewritten = rewrite_query(long_q, history)
    return _check(
        not was_rewritten and rewritten == long_q,
        "长 query 无指代 → 不改写",
        f"result_len={len(rewritten)}",
    )


def case_6_llm_error_fallback():
    """LLM 异常 → 降级返原 query"""
    print("\n[Case 6] LLM 异常降级")
    history = [
        {"role": "user", "content": "iPhone"},
        {"role": "assistant", "content": "好的"},
    ]
    with patch(
        "app.services.query_rewriter.qwen_chat",
        side_effect=Exception("mocked LLM error"),
    ):
        rewritten, was_rewritten = rewrite_query("它能便宜吗", history)
    return _check(
        not was_rewritten and rewritten == "它能便宜吗",
        "LLM 异常 → 降级返原 query",
        f"result='{rewritten}'",
    )


def case_7_empty_query():
    """空 query → 直接返空，不调 LLM"""
    print("\n[Case 7] 空 query")
    rewritten, was_rewritten = rewrite_query("", None)
    ok1 = _check(not was_rewritten, "空 query → not rewritten")
    ok2 = _check(rewritten == "", "空 query → 返空字符串", f"result='{rewritten}'")
    # 也测 None
    rewritten2, was_rewritten2 = rewrite_query(None, None)
    ok3 = _check(
        not was_rewritten2 and rewritten2 is None,
        "None query → 返 None",
        f"result={rewritten2}",
    )
    return ok1 and ok2 and ok3


# =============================================================
# Main
# =============================================================
def main():
    print("=" * 60)
    print("M12 query_rewriter 验证")
    print("=" * 60)

    results = [
        case_1_basic_coreference(),
        case_2_no_coreference(),
        case_3_coref_no_history(),
        case_4_complex_coref(),
        case_5_long_query_no_coref(),
        case_6_llm_error_fallback(),
        case_7_empty_query(),
    ]

    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{total} PASS")
    print("=" * 60)
    if passed < total:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()