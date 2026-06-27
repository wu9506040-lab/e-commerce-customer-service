#!/usr/bin/env python3
"""
M5 端到端验收（PROJECT_DESIGN.md §8）

4 类意图 × 10 用例 = 40 条，通过率门槛 ≥ 85%。

通过条件（每条同时满足）：
  1. SSE 完整（meta + token + done/error 正常）
  2. meta.intent == expected_intent
  3. 答案包含预期关键词（任一 must_contain）
  4. latency 首 token < 3000ms（用总耗时近似）

覆盖维度：
  - 规则命中 vs LLM 兜底（product/policy 各保留 1-2 条触发 LLM）
  - 登录态（order/refund 必须登录，product/policy 不登录）
  - 边界（超期退货、已退款订单再查、规则冲突语序）
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

# 复用 test_chat_e2e.py 的 admin JWT（user_id=1 名下 5 单全在）
ADMIN_JWT = os.environ.get(
    "TEST_JWT",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjowLCJleHAiOjk5OTk5OTk5OTl9.FFI_p8_HU8nprdYak5OiqXsLQv7XyewoJ-SbGGgzh6M",
)

# 数据快照（来自 seed_ecommerce_data.py，2026-06-27）
#   products: SKU001(ZP1 ¥5999) / SKU002(ZP2Pro ¥4299) / SKU003(ZN1 ¥1299) / SKU004(ZN2 ¥899) /
#             SKU005(BP1 耳机 ¥899) / SKU006(WS1 手表 ¥1299) / SKU007(PT1 平板 ¥2499) /
#             SKU008(LB1 笔记本 ¥5499) / SKU009(KB1 键盘 ¥499) / SKU010(MS1 鼠标 ¥199)
#   orders:   ORD20260620001 pending 899   ORD20260621002 paid 6898   ORD20260622003 shipped 1299
#             ORD20260615004 delivered 4299 (签收 10 天)   ORD20260601005 refunded 698

# (query, login, expect_intent, must_contain_any, note)
CASES = [
    # ============== order_query × 10 ==============
    ("ORD20260620001 现在到哪了", True, "order_query", ["ORD20260620001", "待发货", "pending"], "明确订单号 + pending"),
    ("ORD20260621002 啥情况", True, "order_query", ["ORD20260621002", "已支付", "paid"], "明确订单号 + paid"),
    ("ORD20260622003 物流", True, "order_query", ["ORD20260622003", "运输中", "shipped", "深圳"], "明确订单号 + shipped"),
    ("ORD20260615004 物流到哪了", True, "order_query", ["ORD20260615004", "已签收", "delivered", "北京"], "明确订单号 + delivered"),
    ("ORD20260601005 退款进度", True, "refund_query", ["ORD20260601005"], "已退款订单的退款进度查询"),
    ("我的订单有哪些", True, "order_query", ["订单号", "ORD"], "无订单号 + 我的"),
    ("我的订单到哪了", True, "order_query", ["订单"], "无订单号 + 物流意图"),
    ("快递派送中吗", True, "order_query", ["订单", "派送", "物流"], "无订单号 + 快递意图"),
    ("查一下 ORD20260621002", True, "order_query", ["ORD20260621002"], "变体语序：查+订单号"),
    ("我买了啥", True, "order_query", ["订单"], "口语化查询订单列表"),

    # ============== refund_query × 10 ==============
    ("ORD20260622003 能退吗", True, "refund_query", ["ORD20260622003", "7 天", "退货"], "运输中 + 在 7 天内可退"),
    ("ORD20260615004 还能退吗", True, "refund_query", ["ORD20260615004", "超过", "7 天", "不可"], "已签收 10 天 + 超期不可退"),
    ("我想退款", True, "refund_query", ["退款"], "纯规则 + 无订单号（默认走最近一单）"),
    ("这个耳机不想要了能退吗", True, "refund_query", ["BP1", "耳机", "退款", "退货"], "指代性查询（取最近耳机订单）"),
    ("退款多久到账", True, "refund_query", ["到账", "工作日", "支付"], "退款到账时长（policy + tool 融合）"),
    ("怎么申请退货", False, "refund_query", ["申请", "退货", "退款"], "纯流程问（不需登录）"),
    ("商品有问题想退", False, "refund_query", ["问题", "退货", "退款"], "质量问题 + 退货"),
    ("退款的钱退到哪里", False, "refund_query", ["退款", "原路", "支付"], "退款路径政策"),
    ("已经退款了怎么查", True, "refund_query", ["ORD20260601005", "退款"], "已退款订单再查询"),
    ("不想要了能退货吗", True, "refund_query", ["退货", "退款", "7 天"], "口语化退款查询"),

    # ============== product_query × 10 ==============
    ("ZP1 现在多少钱", False, "product_query", ["ZP1", "5999"], "明确 SKU + 价格"),
    ("BP1 续航怎么样", False, "product_query", ["BP1", "耳机", "续航", "降噪"], "SKU + 属性查询"),
    ("你们这有什么手机", False, "product_query", ["手机", "ZP1", "ZP2"], "类目 + 推荐"),
    ("千元机推荐", False, "product_query", ["ZN1", "1299", "千元"], "价格段推荐"),
    ("SKU002 的配置", False, "product_query", ["SKU002", "ZP2", "配置"], "明确 SKU + 配置"),
    ("笔记本有哪些", False, "product_query", ["笔记本", "LB1"], "类目查询"),
    ("键盘有什么推荐", False, "product_query", ["键盘", "KB1"], "类目 + 配件推荐"),
    ("ZP1 和 ZP2 哪个好", False, "product_query", ["ZP1", "ZP2"], "对比查询"),
    ("平板多少钱", False, "product_query", ["PT1", "平板", "2499"], "类目 + 价格"),
    ("鼠标便宜的有吗", False, "product_query", ["MS1", "鼠标", "199"], "类目 + 价格段"),

    # ============== policy_query × 10 ==============
    ("7 天无理由退货运费谁出", False, "policy_query", ["运费", "7 天", "买家", "卖家"], "退货政策细节"),
    ("保修期多久", False, "policy_query", ["保修", "1 年", "整机"], "保修政策（LLM 兜底路径）"),
    ("双十一有什么活动", False, "policy_query", ["活动", "促销", "折扣"], "活动政策（LLM 兜底路径）"),
    ("什么时候发货", False, "policy_query", ["发货", "24 小时", "现货"], "发货时效政策"),
    ("支持哪些支付方式", False, "policy_query", ["微信", "支付宝", "支付"], "支付方式政策"),
    ("运费多少", False, "policy_query", ["运费", "包邮", "元"], "运费政策"),
    ("怎么开发票", False, "policy_query", ["发票", "电子", "申请"], "发票政策"),
    ("会员有什么权益", False, "policy_query", ["会员", "权益", "折扣"], "会员权益政策"),
    ("优惠券怎么用", False, "policy_query", ["优惠券", "使用", "订单"], "优惠券政策"),
    ("电池保修多久", False, "policy_query", ["电池", "保修", "6 个月"], "细分保修政策（电池/配件）"),
]


def call_chat(query: str, jwt: str | None, timeout: int = 30) -> dict:
    """调 /chat SSE，收集所有事件，返回 {ok, meta, answer, first_token_ms, elapsed_ms, tokens, error}"""
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
    first_token_ms: float | None = None

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
                    if first_token_ms is None:
                        first_token_ms = (time.time() - t0) * 1000
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
            "elapsed_ms": (time.time() - t0) * 1000,
            "first_token_ms": None,
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
        "first_token_ms": first_token_ms,
        "tokens_count": len(tokens),
    }


def main():
    print("=" * 80)
    print("M5 端到端验收 — 4 意图 x 10 用例 = 40 条")
    print("标准 (PROJECT_DESIGN.md §8): 通过率 >= 85% + latency < 3s")
    print("=" * 80)

    by_intent: dict[str, list[tuple[bool, str, list[str], float]]] = {}
    fail_cases = []
    pass_cnt = 0
    total = len(CASES)

    for i, (query, login, expect_intent, must_contain_any, note) in enumerate(CASES, 1):
        jwt = ADMIN_JWT if login else None
        r = call_chat(query, jwt)

        intent_got = (r.get("meta") or {}).get("intent", "N/A")
        answer = r.get("answer", "")
        elapsed = r.get("elapsed_ms", 0)
        first_tok = r.get("first_token_ms")

        problems = []
        if not r["ok"]:
            problems.append(f"SSE 异常: {r.get('error', '?')}")
        if intent_got != expect_intent:
            problems.append(f"意图不符 expected={expect_intent} got={intent_got}")
        if must_contain_any and not any(kw in answer for kw in must_contain_any):
            problems.append(f"答案缺关键词（任一）: {must_contain_any}")
        if elapsed > 5000:
            # 总耗时 5s 阈值（§9 「整体完成 < 5s」）
            problems.append(f"总耗时 {elapsed:.0f}ms 超过 5s")
        if first_tok is not None and first_tok > 2000:
            # §9 「首 token < 2s」
            problems.append(f"首 token {first_tok:.0f}ms 超过 2s")

        ok = not problems
        mark = "PASS" if ok else "FAIL"
        ft_str = f"{first_tok:5.0f}ms" if first_tok is not None else "  N/A "
        print(f"\n[{mark}] #{i:2d} intent={intent_got:<13} 1st={ft_str} total={elapsed:5.0f}ms  login={login}  Q: {query}")
        print(f"        note: {note}")
        if answer:
            preview = answer[:120].replace("\n", " ")
            print(f"        A: {preview}{'...' if len(answer) > 120 else ''}")
        for p in problems:
            print(f"        X {p}")

        by_intent.setdefault(expect_intent, []).append((ok, query, must_contain_any, elapsed, first_tok or 0))
        if ok:
            pass_cnt += 1
        else:
            fail_cases.append((i, query, expect_intent, problems))

    print("\n" + "=" * 80)
    print(f"总通过: {pass_cnt}/{total} = {pass_cnt/total*100:.1f}%")
    print(f"M5 验收门槛: >= 85% -> {'PASS' if pass_cnt/total >= 0.85 else 'FAIL'}")
    print("=" * 80)

    print("\n分意图统计:")
    print(f"{'意图':<16} {'通过':>5} {'总数':>5} {'通过率':>8}  {'首 token 均':>10}  {'总耗时均':>10}")
    for intent in ["order_query", "refund_query", "product_query", "policy_query"]:
        stats = by_intent.get(intent, [])
        if not stats:
            continue
        ok_n = sum(1 for s in stats if s[0])
        avg_first = sum(s[4] for s in stats if s[4] > 0) / max(1, sum(1 for s in stats if s[4] > 0))
        avg_total = sum(s[3] for s in stats) / len(stats)
        print(f"{intent:<16} {ok_n:>5} {len(stats):>5} {ok_n/len(stats)*100:>7.1f}%  {avg_first:>8.0f}ms  {avg_total:>8.0f}ms")

    # §9 全局性能统计
    all_first = [s[4] for s in sum(by_intent.values(), []) if s[4] > 0]
    all_total = [s[3] for s in sum(by_intent.values(), [])]
    if all_first:
        all_first.sort()
        all_total.sort()
        def pct(arr, p):
            if not arr: return 0
            k = max(0, min(len(arr) - 1, int(len(arr) * p / 100)))
            return arr[k]
        print("\n§9 性能指标（全局）:")
        print(f"  首 token  P50 = {pct(all_first, 50):>6.0f}ms   P95 = {pct(all_first, 95):>6.0f}ms   目标 < 2000ms")
        print(f"  总耗时    P50 = {pct(all_total, 50):>6.0f}ms   P95 = {pct(all_total, 95):>6.0f}ms   目标 < 5000ms")
        # 达标统计
        first_pass = sum(1 for x in all_first if x < 2000) / len(all_first) * 100
        total_pass = sum(1 for x in all_total if x < 5000) / len(all_total) * 100
        print(f"  首 token 达标率 = {first_pass:>5.1f}%   总耗时达标率 = {total_pass:>5.1f}%")

    if fail_cases:
        print("\n失败用例汇总:")
        for i, q, exp, probs in fail_cases:
            print(f"  #{i}: [{exp}] {q}")
            for p in probs:
                print(f"      - {p}")

    # 退出码：CI 可直接用
    sys.exit(0 if pass_cnt / total >= 0.85 else 1)


if __name__ == "__main__":
    main()