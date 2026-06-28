#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
eval_refund_accuracy.py - 退款准确性专项评估（M9.5 + 用户反馈驱动）

背景：用户反馈"退款咨询答错了，严重质疑 LLM 准确性"。
本脚本系统化测各订单状态 × 多种用户问法 × 期望答案，定位问题。

设计：
- 6 种订单状态（pending/paid/shipped/delivered/refunded/completed）× N 种问法
- 每个 case 含 user_query + expected_keywords（必须出现）+ banned_keywords（不应出现）
- 通过 /api/chat 流式接口，收集最终 answer
- 跑完出报告：PASS/FAIL 列表 + 失败原因

用法：
    # 1) 先 seed 演示数据（脚本会创建覆盖所有状态的订单）
    DATABASE_URL='mysql+pymysql://cs_user:dev_user_2026@localhost:3307/customer_service?charset=utf8mb4' \
    PYTHONIOENCODING=utf-8 PYTHONPATH=backend python scripts/seed_demo_data.py

    # 2) 跑本测试
    PYTHONIOENCODING=utf-8 python scripts/eval_refund_accuracy.py
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

API_BASE = "http://localhost:8000/api"
USERNAME = "demotest"
PASSWORD = "demotest123"

# =============================================================
# 测试用例定义
# =============================================================
# expected_keywords: 至少 1 个必须出现
# banned_keywords: 任何 1 个出现就 FAIL
# =============================================================
TEST_CASES = [
    # ========== delivered 已签收（应该可退）==========
    {
        "name": "delivered-直接问",
        "query": "ORD20260628004 能退货吗",
        "order_no": "ORD20260628004",  # seed 中已签收 5 天前
        "expected_any": ["可以", "支持", "能退", "7 天", "7天", "无理由"],
        "banned_any": ["不能退", "无法退", "不可退", "超过 7 天", "超期", "不支持退货"],
        "note": "已签收 5 天，应在 7 天窗口内，可退",
    },
    {
        "name": "delivered-如何申请",
        "query": "ORD20260628004 怎么退",
        "order_no": "ORD20260628004",
        "expected_any": ["申请", "退货", "流程", "订单"],
        "banned_any": ["不能退", "无法退", "超过 7 天"],
        "note": "已签收 5 天，应引导申请流程",
    },
    {
        "name": "delivered-无order",
        "query": "我想退刚签收的那单",
        "order_no": None,
        "expected_any": ["请提供", "订单号"],
        "banned_any": ["不能退", "超过 7 天"],
        "note": "M9.5 修复后无订单号必须反问，禁止偷换到最近订单",
    },
    # ========== refunded 已退款（不可重复退）==========
    {
        "name": "refunded-已退过",
        "query": "ORD20260628006 能再退一次吗",
        "order_no": "ORD20260628006",
        "expected_any": ["已退款", "无法重复", "不能"],
        "banned_any": ["可以", "支持"],
        "note": "已退过，不可再退",
    },
    # ========== pending 待支付（应引导取消/支付）==========
    {
        "name": "pending-未支付",
        "query": "ORD20260628001 能退吗",
        "order_no": "ORD20260628001",
        "expected_any": ["未支付", "待支付", "取消", "支付"],
        "banned_any": ["超过 7 天", "未签收", "已签收"],
        "note": "未支付订单，应该说还未支付可取消/支付，不是'不能退'",
    },
    # ========== paid 已支付 ==========
    {
        "name": "paid-已支付未发货",
        "query": "ORD20260628002 还能退吗",
        "order_no": "ORD20260628002",
        "expected_any": ["可以", "支持", "申请", "已支付"],
        "banned_any": ["不能退", "已签收", "超过 7 天"],
        "note": "已支付，可申请退款（自动同意）",
    },
    # ========== shipped 运输中 ==========
    {
        "name": "shipped-运输中",
        "query": "ORD20260628003 能退吗",
        "order_no": "ORD20260628003",
        "expected_any": ["可以", "支持", "运输", "申请", "拒收"],
        "banned_any": ["已签收", "超过 7 天", "不能退"],
        "note": "运输中，可以申请退款（拒收）",
    },
    # ========== completed 已完成 ==========
    {
        "name": "completed-已完成",
        "query": "ORD20260628005 可以退吗",
        "order_no": "ORD20260628005",
        "expected_any": ["可以", "支持", "申请", "7 天", "7天"],
        "banned_any": ["超过 7 天", "不能退"],
        "note": "已完成 15 天前，应在 7 天窗口内? 实际看 create_time",
    },
    # ========== 通用问题 ==========
    {
        "name": "policy-退货流程",
        "query": "怎么申请退款",
        "order_no": None,
        "expected_any": ["申请", "退货", "退款", "订单", "流程"],
        "banned_any": [],
        "note": "通用政策问题，应回答流程",
    },
    {
        "name": "policy-运费",
        "query": "7天无理由退货运费谁出",
        "order_no": None,
        "expected_any": ["运费", "承担", "商家", "买家", "责任"],
        "banned_any": [],
        "note": "运费责任问题",
    },
    # ========== M9.5 回归：用户截图里的真实订单号（防串单 + 防幻觉）==========
    {
        "name": "user-reported-已签收单",
        "query": "ORD20260615004 能退款吗",
        "order_no": None,  # 让 LLM 从 query 自己提取（不作弊）
        "expected_any": ["ORD20260615004", "可以", "支持", "签收", "7 天"],
        "banned_any": ["ORD20260620001", "不能退款", "无法退款", "超过 7 天或未签收", "未支付"],
        "note": "用户截图原 case：必须提到 ORD20260615004，禁止串到 ORD20260620001",
    },
    {
        "name": "user-reported-未支付单",
        "query": "ORD20260620001 能退吗",
        "order_no": None,
        "expected_any": ["ORD20260620001", "未支付", "待支付", "取消", "支付"],
        "banned_any": ["ORD20260615004", "不能退", "超过 7 天或未签收"],
        "note": "pending 单：必须提到 ORD20260620001 + 未支付",
    },
    {
        "name": "anti-hallucination-不带订单号",
        "query": "我想退个货，能退吗",
        "order_no": None,
        "expected_any": ["请提供", "订单号"],
        "banned_any": ["可以退", "不能退", "超过 7 天"],
        "note": "无订单号时禁止推测/编造订单，必须反问用户",
    },
    {
        "name": "anti-mixup-指定订单号必须出现在答案里",
        "query": "ORD20260628004 退货运费谁出",
        "order_no": None,
        "expected_any": ["ORD20260628004"],
        "banned_any": ["ORD20260628001", "ORD20260628002", "ORD20260628003", "ORD20260628005", "ORD20260628006"],
        "note": "硬约束 #4：订单号必须与用户问的一致，禁止串单",
    },
]
# =============================================================
# 工具函数
# =============================================================
def login() -> str:
    """登录拿 cookie，返回 cookie 字符串"""
    form = f"username={USERNAME}&password={PASSWORD}"
    req = urllib.request.Request(
        f"{API_BASE}/auth/login",
        data=form.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        cookie = resp.headers.get("Set-Cookie", "")
        return cookie.split(";")[0]  # 只取 cs_token=xxx
    except urllib.error.HTTPError as e:
        print(f"❌ 登录失败: {e.code} {e.read().decode()}", flush=True)
        sys.exit(1)


def chat_stream(query: str, order_no: Optional[str], cookie: str) -> dict:
    """调 /chat 流式接口，合并 token，返回 {intent, refundable, answer}"""
    body = json.dumps({
        "query": query,
        "session_id": None,
        "sku": None,
        "order_no": order_no,
    })
    req = urllib.request.Request(
        f"{API_BASE}/chat",
        data=body.encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cookie": cookie,
        },
        method="POST",
    )
    intent = None
    refundable = None
    answer = ""
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            et = event.get("type")
            if et == "meta":
                intent = event.get("intent")
                refundable = event.get("refundable")
            elif et == "token":
                answer += event.get("text", "")
            elif et == "done":
                break
            elif et == "error":
                answer += f"[ERROR: {event.get('message', '')}]"
                break
        return {"intent": intent, "refundable": refundable, "answer": answer}
    except Exception as e:
        return {"intent": None, "refundable": None, "answer": f"[REQUEST FAILED: {e}]"}


def check_case(case: dict, result: dict) -> tuple[bool, str]:
    """检查结果是否符合预期，返回 (pass, reason)"""
    answer = result["answer"]
    if "[REQUEST FAILED" in answer or "[ERROR" in answer:
        return False, f"请求/解析失败: {answer[:100]}"

    expected_any = case.get("expected_any", [])
    banned_any = case.get("banned_any", [])

    if expected_any and not any(k in answer for k in expected_any):
        return False, f"缺期望关键词 {expected_any}"

    for bad in banned_any:
        if bad in answer:
            return False, f"出现禁用关键词 '{bad}'"

    return True, "OK"


# =============================================================
# 主流程
# =============================================================
def main():
    print("=" * 70)
    print("退款准确性专项测试 (M9.5 + 用户反馈驱动)")
    print("=" * 70)

    print(f"\n[1/3] 登录 {USERNAME}...")
    cookie = login()
    print(f"  cookie: {cookie[:40]}...")

    print(f"\n[2/3] 跑 {len(TEST_CASES)} 个用例...")
    results = []
    for i, case in enumerate(TEST_CASES, 1):
        print(f"\n[{i}/{len(TEST_CASES)}] {case['name']}")
        print(f"  Q: {case['query']}")
        print(f"  order_no: {case.get('order_no')}")
        print(f"  note: {case.get('note', '')}")
        t0 = time.time()
        result = chat_stream(case["query"], case.get("order_no"), cookie)
        elapsed = time.time() - t0
        ok, reason = check_case(case, result)
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status} ({elapsed:.1f}s) intent={result['intent']} refundable={result['refundable']}")
        if not ok:
            print(f"  reason: {reason}")
        print(f"  answer: {result['answer'][:200]}")
        if len(result['answer']) > 200:
            print(f"          ... ({len(result['answer'])} chars total)")
        results.append({
            "case": case,
            "result": result,
            "ok": ok,
            "reason": reason,
        })

    # 汇总
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    failed = total - passed
    print(f"\n  总数: {total}")
    print(f"  通过: {passed}")
    print(f"  失败: {failed}")
    print(f"  通过率: {passed/total*100:.1f}%")

    if failed > 0:
        print("\n  失败用例:")
        for r in results:
            if not r["ok"]:
                print(f"    - {r['case']['name']}: {r['reason']}")

    # 输出 JSON 报告
    report_path = Path(__file__).parent / "eval_refund_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "name": r["case"]["name"],
                    "query": r["case"]["query"],
                    "order_no": r["case"].get("order_no"),
                    "ok": r["ok"],
                    "reason": r["reason"],
                    "intent": r["result"]["intent"],
                    "refundable": r["result"]["refundable"],
                    "answer": r["result"]["answer"],
                }
                for r in results
            ],
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n  报告: {report_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())