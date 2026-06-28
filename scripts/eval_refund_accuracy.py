#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
eval_refund_accuracy.py - 退款准确性专项评估（M9.5 + 用户反馈驱动 + 100% 准确铁律）

背景：用户反馈"退款咨询答错了，严重质疑 LLM 准确性"。
本脚本系统化测各订单状态 × 各种用户问法 × 期望答案，定位问题。

设计铁律：
1. 6 种订单状态（pending/paid/shipped/delivered/refunded/completed）× N 种问法
2. 每个 case 含 user_query + expected_keywords（必须出现）+ banned_keywords（不应出现）
3. 100% 准确：能确定的必须答对；不能确定的返回"请联系人工"或"请提供订单号"
4. 串单防护：用户问 X 单，回答必须提到 X 单，禁止串到其他单号
5. 真实用户口语：包含"行不行/咋办/咋退/推款/退？"等变体

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
# expected_order_no: 用户问的订单号必须出现在答案里（防串单）
# =============================================================
TEST_CASES = [
    # ============================================================
    # A. delivered 已签收 - 应该可退（在 7 天窗口内）
    # ============================================================
    {
        "name": "A1-delivered-直接问-标准说法",
        "query": "ORD20260628004 能退货吗",
        "order_no": None,
        "expected_any": ["可以", "支持", "能退", "签收", "7 天"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能退", "无法退", "不可退", "超过 7 天或未签收", "不支持退货"],
        "note": "delivered 5 天，应在窗口内，必须串到自己",
    },
    {
        "name": "A2-delivered-口语化-行不行",
        "query": "ORD20260628004 这单退货行不行",
        "order_no": None,
        "expected_any": ["可以", "支持", "行", "能退"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能", "无法"],
        "note": "口语化问法",
    },
    {
        "name": "A3-delivered-口语化-咋办",
        "query": "ORD20260628004 咋退啊",
        "order_no": None,
        "expected_any": ["申请", "退款", "退货", "流程"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能", "不支持"],
        "note": "极简口语化",
    },
    {
        "name": "A4-delivered-错别字-推款",
        "query": "ORD20260628004 怎么推款",
        "order_no": None,
        "expected_any": ["退款", "退货", "申请"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能", "不支持", "推款"],
        "note": "错别字应被 LLM 识别为退款",
    },
    {
        "name": "A5-delivered-如何申请流程",
        "query": "ORD20260628004 怎么申请退款，流程是什么",
        "order_no": None,
        "expected_any": ["申请", "退款", "订单", "流程", "步骤"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能", "不支持"],
        "note": "询问流程，应引导申请步骤",
    },
    {
        "name": "A6-delivered-超期用户截图",
        "query": "ORD20260615004 能退款吗",
        "order_no": None,
        "expected_any": ["ORD20260615004", "签收", "超期", "超过 7 天", "无法", "不能"],
        "expected_order_no": "ORD20260615004",
        "banned_any": ["ORD20260620001", "未支付", "待支付", "可以退"],
        "note": "用户截图原 case：delivered 超期（已签收 10 天），必须准确说不能退",
    },

    # ============================================================
    # B. refunded 已退款 - 不可重复退
    # ============================================================
    {
        "name": "B1-refunded-已退过-直接问",
        "query": "ORD20260628006 能再退一次吗",
        "order_no": None,
        "expected_any": ["已退款", "无法重复", "不能", "不可"],
        "expected_order_no": "ORD20260628006",
        "banned_any": ["可以再退", "支持再次退", "支持重新退", "可以重新退", "支持您再次退款"],
        "note": "已退款，不可重复退，banned 精确到'可以再次退'",
    },
    {
        "name": "B2-refunded-口语化-咋办",
        "query": "ORD20260628006 这单退过了又想退咋办",
        "order_no": None,
        "expected_any": ["已退款", "无法", "不能"],
        "expected_order_no": "ORD20260628006",
        "banned_any": ["可以再退", "支持再次退", "支持重新退", "可以重新退"],
        "note": "口语化 + 已退过场景，banned 精确到'可以再次退'",
    },

    # ============================================================
    # C. pending 待支付 - 应引导取消/支付
    # ============================================================
    {
        "name": "C1-pending-未支付-直接问",
        "query": "ORD20260628001 能退吗",
        "order_no": None,
        "expected_any": ["ORD20260628001", "未支付", "待支付", "取消", "支付"],
        "expected_order_no": "ORD20260628001",
        "banned_any": ["超过 7 天", "未签收", "已签收", "签收"],
        "note": "未支付订单：说未支付+可取消/继续支付，禁止套用 delivered 事实",
    },
    {
        "name": "C2-pending-口语化-不想要了",
        "query": "ORD20260628001 不想要了能退吗",
        "order_no": None,
        "expected_any": ["未支付", "待支付", "取消"],
        "expected_order_no": "ORD20260628001",
        "banned_any": ["签收", "已签收"],
        "note": "口语化：未支付不想要",
    },
    {
        "name": "C3-pending-用户截图",
        "query": "ORD20260620001 能退吗",
        "order_no": None,
        "expected_any": ["ORD20260620001", "未支付", "待支付", "取消", "支付"],
        "expected_order_no": "ORD20260620001",
        "banned_any": ["ORD20260615004", "不能退", "签收", "已签收", "超过 7 天或未签收"],
        "note": "用户截图原 case：pending 必须提自己，禁止套 ORD15004 的事实",
    },

    # ============================================================
    # D. paid 已支付 - 可申请退款
    # ============================================================
    {
        "name": "D1-paid-已支付-直接问",
        "query": "ORD20260628002 还能退吗",
        "order_no": None,
        "expected_any": ["ORD20260628002", "可以", "支持", "申请", "已支付"],
        "expected_order_no": "ORD20260628002",
        "banned_any": ["不能退", "已签收", "超过 7 天"],
        "note": "已支付 1 天，可申请退款",
    },

    # ============================================================
    # E. shipped 运输中 - 可申请退款（拒收）
    # ============================================================
    {
        "name": "E1-shipped-运输中-直接问",
        "query": "ORD20260628003 能退吗",
        "order_no": None,
        "expected_any": ["ORD20260628003", "可以", "支持", "运输", "申请"],
        "expected_order_no": "ORD20260628003",
        "banned_any": ["已签收", "超过 7 天", "不能退"],
        "note": "运输中可拒收退款",
    },

    # ============================================================
    # F. completed 已完成 - 视情况（看 create_time 与签收日）
    # ============================================================
    {
        "name": "F1-completed-已完成",
        "query": "ORD20260628005 可以退吗",
        "order_no": None,
        "expected_any": ["ORD20260628005", "可以", "支持", "申请"],
        "expected_order_no": "ORD20260628005",
        "banned_any": ["不能退"],
        "note": "completed 状态：业务规则允许申请退款",
    },

    # ============================================================
    # G. 无订单号 - 必须反问，不准瞎编
    # ============================================================
    {
        "name": "G1-无order-口语化",
        "query": "我想退个货，能退吗",
        "order_no": None,
        "expected_any": ["请提供", "订单号", "ORD"],
        "banned_any": ["您可以退款", "可以为您办理退款", "支持您退款", "已为您办理", "退款审核通过", "无法退款", "不能退款", "已退款", "超过 7 天"],
        "note": "无订单号必须反问，禁止瞎编退款结论",
    },
    {
        "name": "G2-无order-极简",
        "query": "退？",
        "order_no": None,
        "expected_any": ["请提供", "订单号", "ORD"],
        "banned_any": ["可以退", "不能退", "超过 7 天"],
        "note": "极简问法，反问用户",
    },
    {
        "name": "G3-无order-无订单号怎么退",
        "query": "没有订单号怎么退",
        "order_no": None,
        "expected_any": ["提供", "订单号", "联系"],
        "banned_any": ["您可以退", "支持您退", "已为您办理", "退款审核通过"],
        "note": "无订单号：必须反问或转人工，禁止瞎退款结论",
    },

    # ============================================================
    # H. 订单不存在 - 必须明确告知，禁止瞎猜
    # ============================================================
    {
        "name": "H1-订单不存在-直接问",
        "query": "ORD99999999999 能退吗",
        "order_no": None,
        "expected_any": ["不存在", "未找到", "没有", "查不到", "请检查"],
        "banned_any": ["可以退", "超过 7 天", "已签收"],
        "note": "不存在的订单号必须明确告知",
    },
    {
        "name": "H2-订单不存在-跨用户",
        "query": "ORD20260615004 能退吗",  # 此单属 demotest，但用作跨用户测试场景
        "order_no": None,
        "expected_any": ["ORD20260615004"],
        "expected_order_no": "ORD20260615004",
        "banned_any": ["ORD20260628001", "ORD20260628002"],  # 禁止串到其他 demotest 单
        "note": "必须提到用户问的真实单号，禁止串单",
    },

    # ============================================================
    # I. 政策问答 - 通用问题，应给出准确政策
    # ============================================================
    {
        "name": "I1-policy-退货流程",
        "query": "怎么申请退款",
        "order_no": None,
        "expected_any": ["申请", "退货", "退款", "订单", "流程"],
        "banned_any": [],
        "note": "通用政策问题，回答流程",
    },
    {
        "name": "I2-policy-运费",
        "query": "7天无理由退货运费谁出",
        "order_no": None,
        "expected_any": ["运费", "承担", "商家", "买家", "责任"],
        "banned_any": [],
        "note": "运费责任问题",
    },
    {
        "name": "I3-policy-退款时效",
        "query": "退款多久到账",
        "order_no": None,
        "expected_any": ["工作日", "天", "到账", "原路", "退款"],
        "banned_any": [],
        "note": "退款时效问题",
    },
    {
        "name": "I4-policy-保修期",
        "query": "手机保修期多久",
        "order_no": None,
        "expected_any": ["保修", "年", "月"],
        "banned_any": [],
        "note": "政策类问题，应有具体数字",
    },

    # ============================================================
    # J. 跨意图边界 - 物流 + 退款混问
    # ============================================================
    {
        "name": "J1-混问-物流+退款",
        "query": "ORD20260628003 现在到哪了，能退吗",
        "order_no": None,
        "expected_any": ["ORD20260628003", "运输", "退款"],
        "expected_order_no": "ORD20260628003",
        "banned_any": ["ORD20260628004"],
        "note": "混问：先答物流再答退款，必须提自己",
    },

    # ============================================================
    # K. 反串单硬约束
    # ============================================================
    {
        "name": "K1-反串单-指定订单号必现",
        "query": "ORD20260628004 退货运费谁出",
        "order_no": None,
        "expected_any": ["ORD20260628004"],
        "banned_any": ["ORD20260628001", "ORD20260628002", "ORD20260628003", "ORD20260628005", "ORD20260628006"],
        "note": "硬约束：订单号必须与用户问的一致",
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
    expected_order_no = case.get("expected_order_no")

    # 期望关键词：至少 1 个出现
    if expected_any and not any(k in answer for k in expected_any):
        return False, f"缺期望关键词 {expected_any}"

    # 期望订单号：必须出现在答案里
    if expected_order_no and expected_order_no not in answer:
        return False, f"答案未包含用户问的订单号 {expected_order_no}"

    # 禁用关键词：任何一个出现都 FAIL
    for bad in banned_any:
        if bad in answer:
            return False, f"出现禁用关键词 '{bad}'"

    return True, "OK"


# =============================================================
# 主流程
# =============================================================
def main():
    print("=" * 70)
    print("退款准确性专项测试 (M9.5 + 用户反馈驱动 + 100% 准确铁律)")
    print("=" * 70)

    print(f"\n[1/3] 登录 {USERNAME}...")
    cookie = login()
    print(f"  cookie: {cookie[:40]}...")

    # 按类别分组打印
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
                    "expected_order_no": r["case"].get("expected_order_no"),
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
