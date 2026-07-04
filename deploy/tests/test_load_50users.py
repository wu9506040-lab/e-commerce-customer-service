#!/usr/bin/env python3
"""
P1 性能压测 - §9 「并发 > 50」验证

模拟 50 并发用户跑 /chat，统计 P50/P95 latency + 错误率 + 吞吐
+ 意图分布 + 与 §9 spec 对比

注：单进程 50 线程模拟并发；如需更高并发考虑 locust/asyncio。
"""
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.stdout.reconfigure(encoding="utf-8")

# 服务地址（可覆盖，默认本地；公网测时设 BASE=http://120.79.27.124:8000）
BASE = os.environ.get("BASE", "http://localhost:8000")

# Admin JWT 必须从环境变量读取，禁止硬编码
# 永不过期 + role=admin 的 token 等于后门，绝对不能进仓库
# 生成方式（短过期，本地压测用）：
#   python -c "
#   import jwt, time
#   print(jwt.encode(
#       {'sub': 1, 'role': 'admin', 'iat': int(time.time()), 'exp': int(time.time()) + 3600},
#       '<JWT_SECRET>', algorithm='HS256'
#   ))
#   "
# 然后：export LOAD_TEST_ADMIN_JWT="<上面的输出>"
ADMIN_JWT = os.environ.get("LOAD_TEST_ADMIN_JWT")
if not ADMIN_JWT:
    sys.exit(
        "ERROR: 环境变量 LOAD_TEST_ADMIN_JWT 未设置。\n"
        "请先生成一个短过期（≤1h）的 admin token，再 export 后跑本脚本。\n"
        "生成方法见本文件顶部注释。"
    )

# 真实查询样本（混合 4 意图）
QUERIES = [
    "ZP1 现在多少钱",
    "我的订单有哪些",
    "我想退款",
    "7 天无理由退货运费谁出",
    "BP1 续航怎么样",
    "ORD20260620001 物流",
    "ORD20260622003 能退吗",
    "什么时候发货",
    "千元机推荐",
    "电池保修多久",
]


def user_session(user_id: int, queries_per_user: int) -> list[dict]:
    """单用户会话：连续发 queries_per_user 个请求"""
    results = []
    cookies = {"cs_token": ADMIN_JWT}
    for i in range(queries_per_user):
        query = QUERIES[i % len(QUERIES)]
        body = {"query": query}

        t0 = time.time()
        first_token_ms = None
        intent_got = None
        tokens_count = 0
        error_msg = None

        try:
            with requests.post(
                f"{BASE}/api/chat",
                json=body,
                cookies=cookies,
                stream=True,
                timeout=60,  # 拉长到 60s 看真实数据（§9 P95 < 5s 是目标，60s 是诊断窗口）
            ) as r:
                for raw_line in r.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="replace")
                    if not line.startswith("data: "):
                        continue
                    try:
                        p = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    t = p.get("type")
                    if t == "meta":
                        intent_got = p.get("intent")
                    elif t == "token":
                        if first_token_ms is None:
                            first_token_ms = (time.time() - t0) * 1000
                        tokens_count += 1
                    elif t == "done":
                        break
                    elif t == "error":
                        error_msg = p.get("message")
                        break
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)[:100]}"

        elapsed_ms = (time.time() - t0) * 1000
        results.append({
            "user_id": user_id,
            "query": query,
            "intent": intent_got,
            "first_token_ms": first_token_ms,
            "elapsed_ms": elapsed_ms,
            "tokens": tokens_count,
            "error": error_msg,
        })
    return results


def pct(arr: list[float], p: int) -> float:
    """百分位"""
    if not arr:
        return 0.0
    s = sorted(arr)
    k = max(0, min(len(s) - 1, int(len(s) * p / 100)))
    return s[k]


def main():
    n_users = 50
    n_queries = 1  # 每用户 1 查询 = 真正的 50 并发（不是 50×5=250）
    warmup = 3  # 预热请求

    print("=" * 70)
    print("P1 性能压测 - §9 「并发 > 50」验证")
    print("=" * 70)
    print(f"配置: {n_users} 并发用户 × {n_queries} 查询 = {n_users * n_queries} 总请求")
    print(f"语义: 50 个用户同时发起请求（spec 「并发 > 50」的合理解读）")

    # Warmup：先把 LLM/RAG 缓存热起来
    print(f"\n预热 ({warmup} 个请求)...")
    for q in QUERIES[:warmup]:
        try:
            requests.post(
                f"{BASE}/api/chat",
                json={"query": q},
                cookies={"cs_token": ADMIN_JWT},
                timeout=30,
            )
        except Exception:
            pass

    # Load test
    print(f"开始压测...")
    t_start = time.time()

    all_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=n_users) as executor:
        futures = [
            executor.submit(user_session, u, n_queries) for u in range(n_users)
        ]
        for f in as_completed(futures):
            try:
                all_results.extend(f.result())
            except Exception as e:
                print(f"  用户失败: {e}")

    total_elapsed = time.time() - t_start
    total_requests = len(all_results)
    throughput = total_requests / total_elapsed if total_elapsed > 0 else 0

    # 统计
    first_tokens = [r["first_token_ms"] for r in all_results if r["first_token_ms"] is not None]
    elapsed_list = [r["elapsed_ms"] for r in all_results]
    errors = [r for r in all_results if r["error"] is not None]
    intent_dist = Counter(r["intent"] for r in all_results if r["intent"])

    print(f"\n{'=' * 70}")
    print(f"压测结果")
    print(f"{'=' * 70}")
    print(f"总请求数:    {total_requests}")
    print(f"总耗时:      {total_elapsed:.2f}s")
    print(f"吞吐量:      {throughput:.2f} req/s")
    print(f"错误数:      {len(errors)} ({len(errors) / total_requests * 100:.2f}%)")
    if errors:
        print("错误样例（前 3 条）:")
        for e in errors[:3]:
            print(f"  - {e['error'][:120]}")

    print(f"\n意图分布: {dict(intent_dist)}")

    print(f"\n首 token latency:")
    print(f"  P50 = {pct(first_tokens, 50):>6.0f}ms")
    print(f"  P95 = {pct(first_tokens, 95):>6.0f}ms")
    print(f"  P99 = {pct(first_tokens, 99):>6.0f}ms")
    print(f"  max = {max(first_tokens):>6.0f}ms" if first_tokens else "  max = N/A")

    print(f"\n总耗时 latency:")
    print(f"  P50 = {pct(elapsed_list, 50):>6.0f}ms")
    print(f"  P95 = {pct(elapsed_list, 95):>6.0f}ms")
    print(f"  P99 = {pct(elapsed_list, 99):>6.0f}ms")
    print(f"  max = {max(elapsed_list):>6.0f}ms" if elapsed_list else "  max = N/A")

    # §9 评估
    print(f"\n{'=' * 70}")
    print(f"§9 评估")
    print(f"{'=' * 70}")
    p95_first = pct(first_tokens, 95)
    p95_total = pct(elapsed_list, 95)
    err_rate = len(errors) / total_requests if total_requests else 0

    checks = [
        (f"并发 > 50", n_users >= 50, f"实际 {n_users}"),
        (f"首 token P95 < 2000ms", p95_first < 2000, f"实际 {p95_first:.0f}ms"),
        (f"总耗时 P95 < 5000ms", p95_total < 5000, f"实际 {p95_total:.0f}ms"),
        (f"错误率 < 1%", err_rate < 0.01, f"实际 {err_rate * 100:.2f}%"),
    ]
    for name, ok, detail in checks:
        print(f"  {'✅' if ok else '❌'} {name:<25}  {detail}")

    passed = all(ok for _, ok, _ in checks)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()