"""
M12 query_rewriter 验证（mock LLM 版，不依赖 API key）

跑全部 7 case：基础逻辑（不调 LLM）+ mock LLM（模拟改写成功）
"""
import sys
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.services.query_rewriter import rewrite_query  # noqa: E402

PASS = "[PASS]"
FAIL = "[FAIL]"


def _check(condition: bool, name: str, detail: str = "") -> bool:
    mark = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {mark} {name}{suffix}")
    return condition


def mock_llm_rewrite_success(messages, **kwargs):
    """模拟 LLM 成功改写：返回模拟结果"""
    # 找到 user prompt 里 "当前问题：" 后面的内容
    user_msg = messages[-1]["content"]
    # 解析 "当前问题：xxx"
    if "当前问题：" in user_msg:
        orig = user_msg.split("当前问题：")[1].split("\n\n改写后：")[0].strip()
    else:
        orig = "unknown"

    # 模拟改写：把"它/这个/那个"替换为 "iPhone 15 Pro"
    if "它" in orig:
        rewritten = orig.replace("它", "iPhone 15 Pro")
    elif "这个" in orig and "那个" in orig:
        rewritten = orig.replace("这个", "iPhone 15 Pro").replace("那个", "华为 Mate 60")
    else:
        rewritten = orig + " (改写)"
    return {"reply": rewritten, "model": "mock", "usage": {}}


def run_all():
    results = []

    # Case 1: 含指代 + 有 history（mock 改写成功）
    print("\n[Case 1] 含指代 + 有 history (mock LLM)")
    history = [
        {"role": "user", "content": "想看看 iPhone 15 Pro"},
        {"role": "assistant", "content": "iPhone 15 Pro 售价 8999"},
    ]
    with patch("app.services.query_rewriter.qwen_chat", side_effect=mock_llm_rewrite_success):
        rewritten, was_rewritten = rewrite_query("它能便宜点吗", history)
    results.append(_check(was_rewritten, "was_rewritten=True", f"result='{rewritten}'"))
    results.append(_check(
        "iphone" in rewritten.lower() or "iPhone" in rewritten,
        "改写结果含 iPhone",
        f"result='{rewritten}'",
    ))

    # Case 2: 无指代 → 跳过
    print("\n[Case 2] 无指代词")
    rewritten, was_rewritten = rewrite_query("续航怎么样", [{"role": "user", "content": "x"}])
    results.append(_check(
        not was_rewritten and rewritten == "续航怎么样",
        "无指代词 → 不改写",
        f"result='{rewritten}'",
    ))

    # Case 3: 有指代无 history → 跳过
    print("\n[Case 3] 含指代 + 无 history")
    rewritten, was_rewritten = rewrite_query("它能便宜吗", None)
    results.append(_check(
        not was_rewritten and rewritten == "它能便宜吗",
        "无 history → 不改写",
        f"result='{rewritten}'",
    ))

    # Case 4: 复杂指代
    print("\n[Case 4] 复杂指代 (mock LLM)")
    history = [
        {"role": "user", "content": "iPhone 15 Pro 和华为 Mate 60 哪个好"},
        {"role": "assistant", "content": "两款都不错"},
    ]
    with patch("app.services.query_rewriter.qwen_chat", side_effect=mock_llm_rewrite_success):
        rewritten, was_rewritten = rewrite_query("这个和那个哪个好", history)
    results.append(_check(
        was_rewritten,
        "复杂指代 → 改写",
        f"result='{rewritten}'",
    ))

    # Case 5: 长 query 无指代 → 跳过
    print("\n[Case 5] 长 query 无指代")
    long_q = "请问 iPhone 15 Pro 的电池续航时间和充电速度如何"
    rewritten, was_rewritten = rewrite_query(long_q, [{"role": "user", "content": "x"}])
    results.append(_check(
        not was_rewritten and rewritten == long_q,
        "长 query 无指代 → 不改写",
    ))

    # Case 6: LLM 异常 → 降级
    print("\n[Case 6] LLM 异常降级")
    with patch(
        "app.services.query_rewriter.qwen_chat",
        side_effect=Exception("mocked LLM error"),
    ):
        rewritten, was_rewritten = rewrite_query("它能便宜吗", [{"role": "user", "content": "x"}])
    results.append(_check(
        not was_rewritten and rewritten == "它能便宜吗",
        "LLM 异常 → 降级返原 query",
        f"result='{rewritten}'",
    ))

    # Case 7: 空 query
    print("\n[Case 7] 空 query")
    r1, w1 = rewrite_query("", None)
    results.append(_check(not w1 and r1 == "", "空字符串 → 返空", f"result='{r1}'"))
    r2, w2 = rewrite_query(None, None)
    results.append(_check(not w2 and r2 is None, "None → 返 None", f"result={r2}"))

    # Case 8 (额外): LLM 返回过长结果 → 降级
    print("\n[Case 8] LLM 输出过长降级")
    def mock_too_long(messages, **kwargs):
        return {"reply": "x" * 1000, "model": "mock", "usage": {}}
    with patch("app.services.query_rewriter.qwen_chat", side_effect=mock_too_long):
        rewritten, was_rewritten = rewrite_query("它能便宜吗", [{"role": "user", "content": "x"}])
    results.append(_check(
        not was_rewritten and rewritten == "它能便宜吗",
        "输出过长 → 降级返原 query",
        f"result_len={len(rewritten)}",
    ))

    # Case 9 (额外): LLM 返回空字符串 → 降级
    print("\n[Case 9] LLM 返回空 → 降级")
    def mock_empty(messages, **kwargs):
        return {"reply": "", "model": "mock", "usage": {}}
    with patch("app.services.query_rewriter.qwen_chat", side_effect=mock_empty):
        rewritten, was_rewritten = rewrite_query("它能便宜吗", [{"role": "user", "content": "x"}])
    results.append(_check(
        not was_rewritten and rewritten == "它能便宜吗",
        "返回空 → 降级返原 query",
        f"result='{rewritten}'",
    ))

    return results


def main():
    print("=" * 60)
    print("M12 query_rewriter 验证 (mock LLM)")
    print("=" * 60)
    results = run_all()
    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{total} PASS")
    print("=" * 60)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()