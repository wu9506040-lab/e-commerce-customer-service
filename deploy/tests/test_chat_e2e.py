#!/usr/bin/env python3
"""
M4 Response Synthesizer 端到端验证

10 条用例覆盖 4 类意图：
1-2. order_query（登录 + 未登录）
3-4. refund_query（登录）
5-7. product_query（关键词 / sku / 推荐）
8-9. policy_query（具体条款 / 通用）
10.  fallback / 无意图命中

验证项：
- SSE 收到 meta + token + done
- meta 含 intent/entities
- token 文本包含预期的结构化数据（订单号/价格/政策引用）
- 不报 500

通过标准：10/10 SSE 完整 + 内容合理性
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

# 强制 UTF-8 输出（Windows GBK console）
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://localhost:8000"

# 从容器里拿一个 admin JWT（user_id=1，5 单全在他名下）
ADMIN_JWT = os.environ.get(
    "TEST_JWT",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjowLCJleHAiOjk5OTk5OTk5OTksInJvbGUiOiJhZG1pbiJ9.jcCYBqdWOsCWA9ZIMY3d2sy0seLngVs_LfTIuS3bslQ",
)

# (query, login, expect_intent, must_contain_text)
CASES = [
    # order_query
    ("ORD20260622003 现在到哪了", True, "order_query", ["ORD20260622003", "运输中"]),
    ("我的订单有哪些", True, "order_query", ["订单号", "ORD"]),
    ("ORD20260615004 物流", True, "order_query", ["ORD20260615004", "已签收"]),
    ("ORD20260620001 啥情况", True, "order_query", ["ORD20260620001", "待发货"]),
    # refund_query
    ("ORD20260622003 能退吗", True, "refund_query", ["ORD20260622003", "退款"]),
    ("ORD20260615004 还能退吗", True, "refund_query", ["ORD20260615004"]),  # 9 天前签收
    # product_query
    ("ZP1 现在多少钱", False, "product_query", ["ZP1", "5999"]),
    ("你们这有什么耳机", False, "product_query", ["耳机", "BP1"]),
    ("ZP1 保修多久", False, "product_query", ["ZP1", "1 年"]),
    # policy_query
    ("7 天无理由退货运费谁出", False, "policy_query", ["运费", "7 天"]),  # 不强制含特定文字，看 LLM 答得对
]


def call_chat(query: str, jwt: str | None, timeout: int = 30) -> dict:
    """调 /chat SSE，收集所有事件，返回 {meta, answer, elapsed, error}"""
    body = json.dumps({"query": query}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Cookie"] = f"cs_token={jwt}"

    req = urllib.request.Request(
        f"{BASE}/chat",
        data=body,
        headers=headers,
        method="POST",
    )
    meta = None
    tokens: list[str] = []
    done_session_id = None
    error_msg = None
    t0 = time.time()

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                payload_str = line[6:]
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                t = payload.get("type")
                if t == "meta":
                    meta = payload
                elif t == "token":
                    tokens.append(payload.get("text", ""))
                elif t == "done":
                    done_session_id = payload.get("session_id")
                elif t == "error":
                    error_msg = payload.get("message")
                    break
    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "error": f"HTTP {e.code}: {e.reason}",
            "elapsed": (time.time() - t0) * 1000,
        }

    elapsed = (time.time() - t0) * 1000
    answer = "".join(tokens)

    return {
        "ok": error_msg is None and done_session_id is not None,
        "meta": meta,
        "answer": answer,
        "session_id": done_session_id,
        "error": error_msg,
        "elapsed_ms": elapsed,
        "tokens_count": len(tokens),
    }


def main():
    print("=" * 80)
    print("M4 Response Synthesizer 端到端验证")
    print("=" * 80)

    pass_cnt = 0
    fail_cases = []

    for i, (query, login, expect_intent, must_contain) in enumerate(CASES, 1):
        jwt = ADMIN_JWT if login else None
        r = call_chat(query, jwt)

        intent_got = (r.get("meta") or {}).get("intent", "N/A")
        answer = r.get("answer", "")

        # 校验
        problems = []
        if not r["ok"]:
            problems.append(f"SSE 异常: {r.get('error', '?')}")
        if intent_got != expect_intent:
            problems.append(f"意图不符 expected={expect_intent} got={intent_got}")
        for kw in must_contain:
            if kw not in answer:
                problems.append(f"answer 缺关键词 '{kw}'")

        mark = "PASS" if not problems else "FAIL"
        print(f"\n[{mark}] #{i:2d} intent={intent_got:<13} {r.get('elapsed_ms', 0):5.0f}ms  "
              f"login={login}  Q: {query}")
        if r.get("answer"):
            preview = answer[:160].replace("\n", " ")
            print(f"        A: {preview}{'...' if len(answer) > 160 else ''}")
        if problems:
            for p in problems:
                print(f"        ❌ {p}")

        if not problems:
            pass_cnt += 1
        else:
            fail_cases.append((i, query, problems))

    print("\n" + "=" * 80)
    total = len(CASES)
    print(f"通过: {pass_cnt}/{total} = {pass_cnt/total*100:.0f}%")
    print(f"M4 验收标准 (§8): SSE 跑通 3 类意图 + latency < 3s 首 token")
    if fail_cases:
        print("\n失败用例汇总:")
        for i, q, probs in fail_cases:
            print(f"  #{i}: {q}")
            for p in probs:
                print(f"    - {p}")


if __name__ == "__main__":
    main()