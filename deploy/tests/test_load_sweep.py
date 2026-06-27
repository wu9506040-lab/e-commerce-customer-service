#!/usr/bin/env python3
"""
P1 性能 sweep - 找 §9 spec 下系统能稳定支撑的最大并发数

依次跑 5/10/20/30/50 并发（每档 1 query）测 P95 总耗时
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

import requests

sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://localhost:8000"
ADMIN_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxIiwiaWF0IjowLCJleHAiOjk5OTk5OTk5OTl9."
    "FFI_p8_HU8nprdYak5OiqXsLQv7XyewoJ-SbGGgzh6M"
)

QUERIES = [
    "ZP1 现在多少钱",
    "我的订单有哪些",
    "我想退款",
    "7 天无理由退货运费谁出",
    "BP1 续航怎么样",
]


def one_query(q):
    t0 = time.time()
    first_ms = None
    intent = None
    err = None
    try:
        with requests.post(
            f"{BASE}/chat",
            json={"query": q},
            cookies={"cs_token": ADMIN_JWT},
            stream=True,
            timeout=60,
        ) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8", errors="replace")
                if not line.startswith("data: "):
                    continue
                try:
                    p = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                t = p.get("type")
                if t == "meta":
                    intent = p.get("intent")
                elif t == "token":
                    if first_ms is None:
                        first_ms = (time.time() - t0) * 1000
                elif t == "done":
                    break
                elif t == "error":
                    err = p.get("message")
                    break
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:80]}"
    return {"first_ms": first_ms, "total_ms": (time.time() - t0) * 1000, "intent": intent, "err": err}


def run_concurrent(n):
    print(f"\n--- {n} 并发用户 ---")
    # warmup
    try:
        requests.post(f"{BASE}/chat", json={"query": "你好"}, cookies={"cs_token": ADMIN_JWT}, timeout=30)
    except Exception:
        pass

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(one_query, QUERIES[i % len(QUERIES)]) for i in range(n)]
        results = [f.result() for f in as_completed(futures)]
    total_elapsed = time.time() - t0

    firsts = [r["first_ms"] for r in results if r["first_ms"] is not None]
    totals = sorted([r["total_ms"] for r in results])
    errors = [r for r in results if r["err"]]

    def pct(arr, p):
        if not arr:
            return 0
        k = max(0, min(len(arr) - 1, int(len(arr) * p / 100)))
        return arr[k]

    err_rate = len(errors) / n * 100
    p50_total = pct(totals, 50)
    p95_total = pct(totals, 95)
    p95_first = pct(firsts, 95) if firsts else 0

    status = "✅" if p95_total < 5000 and err_rate < 1 else "❌"
    print(f"  {status} P50总耗时={p50_total:.0f}ms  P95总耗时={p95_total:.0f}ms  P95首token={p95_first:.0f}ms  错误率={err_rate:.1f}%  吞吐={n/total_elapsed:.2f} req/s")

    if errors:
        print(f"    错误样例: {errors[0]['err'][:80]}")
    return {"n": n, "p95_total": p95_total, "err_rate": err_rate, "status": status}


def main():
    print("=" * 70)
    print("P1 并发 sweep - 找 §9 spec 下最大稳定并发")
    print("§9 目标: P95总耗时 < 5000ms + 错误率 < 1%")
    print("=" * 70)

    levels = [5, 10, 20, 30, 50]
    results = []
    for n in levels:
        results.append(run_concurrent(n))
        # 给后端恢复时间，避免 back-to-back 累积
        time.sleep(5)

    print("\n" + "=" * 70)
    print("Sweep 总结")
    print("=" * 70)
    print(f"{'并发':>5} {'P95 总耗时':>12} {'错误率':>8} {'达标':>5}")
    for r in results:
        print(f"{r['n']:>5} {r['p95_total']:>10.0f}ms {r['err_rate']:>7.1f}% {r['status']:>5}")

    # 找最大达标并发
    max_pass = max((r["n"] for r in results if r["status"] == "✅"), default=0)
    print(f"\n§9 实测最大稳定并发: {max_pass} (spec 写 > 50，目前实测 {max_pass}/50 = {max_pass/50*100:.0f}%)")


if __name__ == "__main__":
    main()