#!/usr/bin/env python3
"""
M3 Intent Classifier 端到端验证

10 条测试用例覆盖 4 类意图：
- refund_query (2)
- order_query (3)
- product_query (3)
- policy_query (2) — 其中 1 条 fallback 触发 LLM

性能要求：规则命中 < 100ms
"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://localhost:8000"

# (query, expected_intent, note)
CASES = [
    # refund_query: 规则命中
    ("我想退款", "refund_query", "纯规则"),
    ("已经签收 5 天了还能退货吗", "refund_query", "纯规则"),
    # order_query: 规则命中
    ("我的订单到哪了", "order_query", "纯规则"),
    ("ORD123 发货了吗", "order_query", "纯规则 + entity=ORD123"),
    ("快递派送中吗", "order_query", "纯规则"),
    # product_query: 规则命中
    ("ZP1 现在多少钱", "product_query", "纯规则 + entity=ZP1"),
    ("BP1 的续航怎么样", "product_query", "纯规则 + entity=BP1"),
    ("你们这有没有手机", "product_query", "纯规则"),
    # policy_query: 部分规则命中 + 部分 LLM 兜底
    ("保修期多久", "policy_query", "规则未命中 → LLM"),
    ("双十一有什么活动", "policy_query", "规则未命中 → LLM"),
]


def call_intent(query: str) -> dict:
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/intent/classify",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("=" * 70)
    print("M3 Intent Classifier 验证")
    print("=" * 70)

    rule_hits = 0
    llm_hits = 0
    pass_cnt = 0
    fail_cases = []

    for i, (query, expected, note) in enumerate(CASES, 1):
        try:
            t0 = time.time()
            r = call_intent(query)
            elapsed = (time.time() - t0) * 1000

            ok = r["intent"] == expected
            mark = "PASS" if ok else "FAIL"
            print(f"[{mark}] #{i:2d} {query[:30]:<30} → {r['intent']:<14} "
                  f"({r['method']:<7}, conf={r['confidence']:.2f}, {elapsed:5.0f}ms) "
                  f"entity={r['entities']} [{note}]")

            if ok:
                pass_cnt += 1
                if r["method"] == "rule":
                    rule_hits += 1
                else:
                    llm_hits += 1
            else:
                fail_cases.append((i, query, expected, r["intent"]))

        except urllib.error.HTTPError as e:
            print(f"[FAIL] #{i:2d} {query[:30]}: HTTP {e.code} {e.reason}")
            fail_cases.append((i, query, expected, f"HTTP {e.code}"))
        except Exception as e:
            print(f"[FAIL] #{i:2d} {query[:30]}: {type(e).__name__}: {e}")
            fail_cases.append((i, query, expected, str(e)))

    print("=" * 70)
    total = len(CASES)
    acc = pass_cnt / total * 100
    print(f"准确率: {pass_cnt}/{total} = {acc:.0f}%")
    print(f"  规则命中: {rule_hits}, LLM 兜底: {llm_hits}")
    print(f"  M3 验收标准 (§8): ≥ 80% → {'✅ PASS' if acc >= 80 else '❌ FAIL'}")

    if fail_cases:
        print("\n失败用例:")
        for i, q, exp, got in fail_cases:
            print(f"  #{i}: '{q}' expected={exp} got={got}")


if __name__ == "__main__":
    main()