#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
eval_refund_accuracy.py - 退款准确性专项评估（M9.5 + 用户反馈驱动）

100% 准确铁律：
1. 能确定的必须答对（meta.refundable + 答案都要对）
2. 不能确定的：要么"请提供订单号"，要么"请联系人工客服"兜底
3. 第二轮对话必须从历史提取 order_no（会话缓存）

断言分 3 层：
- meta.refundable 必须 = 期望值（True / False / None=反问）
- 答案必须含"退款结论句"：可以退 / 不能退 / 请提供订单号 / 请联系人工
- 答案必须含期望订单号（防串单）

用法：
    DATABASE_URL='mysql+pymysql://cs_user:dev_user_2026@localhost:3307/customer_service?charset=utf8mb4' \
    PYTHONIOENCODING=utf-8 PYTHONPATH=backend python scripts/seed_demo_data.py

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
# 测试用例
# 字段：
#   query: 用户输入
#   order_no: 透传给后端的 order_no（None = 让 LLM 自己提取）
#   session_id: 复用已有会话（多轮测试）
#   expected_refundable: 期望 meta.refundable (True/False/None)
#   expected_verdict_any: 答案必须含的"退款结论"关键词之一
#   expected_order_no: 答案必须包含的订单号（防串单）
#   banned_any: 答案绝对不能含的关键词
#   note: 说明
# =============================================================
TEST_CASES = [
    # ============= A. delivered（5 天前签收，窗口内）=============
    {
        "name": "A1-delivered-直接问",
        "query": "ORD20260628004 能退货吗",
        "expected_refundable": True,
        "expected_verdict_any": ["可以退", "支持退", "能退", "符合 7 天"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能退", "无法退", "超过 7 天或未签收", "请提供订单号"],
        "note": "delivered 5 天，可退，必须含订单号",
    },
    {
        "name": "A2-delivered-口语化",
        "query": "ORD20260628004 咋退啊",
        "expected_refundable": True,
        "expected_verdict_any": ["可以", "支持", "能退", "申请"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能退", "无法退", "请提供订单号"],
        "note": "口语化问法",
    },
    {
        "name": "A3-delivered-错别字-推款",
        "query": "ORD20260628004 怎么推款",
        "expected_refundable": True,
        "expected_verdict_any": ["退款", "退货", "申请"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能", "不支持", "请提供订单号"],
        "note": "错别字",
    },
    {
        "name": "A4-delivered-申请流程",
        "query": "ORD20260628004 怎么申请退款，流程是什么",
        "expected_refundable": True,
        "expected_verdict_any": ["申请", "退款", "订单", "流程", "步骤"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["不能", "不支持"],
        "note": "问流程",
    },

    # ============= A. delivered 超期（10 天前签收，超 7 天）=============
    {
        "name": "A5-delivered超期-用户截图",
        "query": "ORD20260615004 能退款吗",
        "expected_refundable": False,
        "expected_verdict_any": ["不能", "无法", "超期", "超过 7 天"],
        "expected_order_no": "ORD20260615004",
        "banned_any": ["ORD20260620001", "未支付", "待支付", "可以退"],
        "note": "delivered 10 天，超期不可退，禁止串到 pending 单",
    },

    # ============= B. refunded 已退过 =============
    {
        "name": "B1-refunded-已退过",
        "query": "ORD20260628006 能再退一次吗",
        "expected_refundable": False,
        "expected_verdict_any": ["已退款", "无法重复", "不能再次", "不可重复"],
        "expected_order_no": "ORD20260628006",
        "banned_any": ["可以再退", "支持再次退", "可以重新退"],
        "note": "已退过，不可重复",
    },

    # ============= C. pending 未支付 =============
    {
        "name": "C1-pending-未支付-直接问",
        "query": "ORD20260628001 能退吗",
        "expected_refundable": True,
        "expected_verdict_any": ["未支付", "待支付", "取消", "支付"],
        "expected_order_no": "ORD20260628001",
        "banned_any": ["签收", "已签收", "超过 7 天或未签收"],
        "note": "未支付应引导取消/支付",
    },
    {
        "name": "C2-pending-用户截图",
        "query": "ORD20260620001 能退吗",
        "expected_refundable": True,
        "expected_verdict_any": ["未支付", "待支付", "取消", "支付"],
        "expected_order_no": "ORD20260620001",
        "banned_any": ["ORD20260615004", "签收", "已签收", "超过 7 天或未签收"],
        "note": "用户截图原 case：pending 必须提自己，禁止套 delivered 事实",
    },

    # ============= D. paid 已支付 =============
    {
        "name": "D1-paid-已支付",
        "query": "ORD20260628002 还能退吗",
        "expected_refundable": True,
        "expected_verdict_any": ["可以", "支持", "申请", "已支付"],
        "expected_order_no": "ORD20260628002",
        "banned_any": ["不能退", "已签收", "超过 7 天"],
        "note": "已支付 1 天，可申请退款",
    },

    # ============= E. shipped 运输中 =============
    {
        "name": "E1-shipped-运输中",
        "query": "ORD20260628003 能退吗",
        "expected_refundable": True,
        "expected_verdict_any": ["可以", "支持", "运输", "申请"],
        "expected_order_no": "ORD20260628003",
        "banned_any": ["已签收", "超过 7 天", "不能退"],
        "note": "运输中可拒收退款",
    },

    # ============= F. completed 已完成 =============
    {
        "name": "F1-completed",
        "query": "ORD20260628005 可以退吗",
        "expected_refundable": True,
        "expected_verdict_any": ["可以", "支持", "申请"],
        "expected_order_no": "ORD20260628005",
        "banned_any": ["不能退"],
        "note": "completed 可申请",
    },

    # ============= G. 无订单号 =============
    {
        "name": "G1-无order-口语化",
        "query": "我想退个货，能退吗",
        "expected_refundable": None,
        "expected_verdict_any": ["请提供", "订单号", "联系人工"],
        "banned_any": ["可以退", "支持您退", "已为您办理", "退款审核通过", "不能退", "超过 7 天"],
        "note": "无订单号必须反问或转人工",
    },
    {
        "name": "G2-无order-极简",
        "query": "退？",
        "expected_refundable": None,
        "expected_verdict_any": ["请提供", "订单号", "联系人工"],
        "banned_any": ["可以退", "不能退"],
        "note": "极简问法，反问或转人工",
    },
    {
        "name": "G3-无order-没有订单号",
        "query": "没有订单号怎么退",
        "expected_refundable": None,
        "expected_verdict_any": ["请提供", "订单号", "联系人工", "提供"],
        "banned_any": ["您可以退", "支持您退", "已为您办理"],
        "note": "无订单号：反问或转人工",
    },

    # ============= H. 订单不存在 =============
    {
        "name": "H1-订单不存在",
        "query": "ORD99999999999 能退吗",
        "expected_refundable": False,
        "expected_verdict_any": ["不存在", "未找到", "没有", "查不到", "请检查", "请联系"],
        "banned_any": ["可以退", "超过 7 天", "已签收", "支持您"],
        "note": "不存在的订单必须明确告知",
    },

    # ============= I. 政策问答 =============
    {
        "name": "I1-policy-运费",
        "query": "7天无理由退货运费谁出",
        "expected_refundable": None,
        "expected_verdict_any": ["运费", "承担", "商家", "买家"],
        "banned_any": ["超过 7 天或未签收", "已签收"],
        "note": "政策问答",
    },
    {
        "name": "I2-policy-退款时效",
        "query": "退款多久到账",
        "expected_refundable": None,
        "expected_verdict_any": ["工作日", "天", "到账", "原路", "退款"],
        "banned_any": [],
        "note": "退款时效",
    },
    {
        "name": "I3-policy-保修期",
        "query": "手机保修期多久",
        "expected_refundable": None,
        "expected_verdict_any": ["保修", "年", "月"],
        "banned_any": [],
        "note": "保修期",
    },

    # ============= J. 反串单硬约束 =============
    {
        "name": "J1-反串单",
        "query": "ORD20260628004 退货运费谁出",
        "expected_refundable": None,
        "expected_verdict_any": ["ORD20260628004"],
        "banned_any": ["ORD20260628001", "ORD20260628002", "ORD20260628003", "ORD20260628005", "ORD20260628006"],
        "note": "订单号必须与用户问的一致",
    },

    # ============= K. 多轮对话（关键！会话缓存）=============
    # 第一轮：用户问 "ORD20260628004 啥情况" → 订单基本信息
    # 第二轮：用户问 "那能退吗" → 必须从 history 提取 ORD20260628004
    # 预期：meta.refundable=True，答案含 ORD20260628004 + "可以退"
    # 这一组在主流程里单独跑（special: multi-turn）
    {
        "name": "K1-多轮第一轮-订单详情",
        "query": "ORD20260628004 啥情况",
        "expected_refundable": None,  # 第一轮是 order_query
        "expected_verdict_any": ["ORD20260628004"],
        "banned_any": [],
        "note": "【多轮会话第 1 轮】先建立订单上下文",
        "_multi_turn_seed": True,  # 标记：seed 一个 session_id 供后续 case 复用
    },
    {
        "name": "K2-多轮第二轮-不带订单号-能退吗",
        "query": "那能退吗",
        "expected_refundable": True,
        "expected_verdict_any": ["可以", "支持", "能退", "申请"],
        "expected_order_no": "ORD20260628004",
        "banned_any": ["请提供订单号", "ORD20260628001", "ORD20260628002", "ORD20260628003"],
        "note": "【多轮会话第 2 轮】不带订单号，必须从 history 提取 ORD20260628004",
        "_multi_turn_followup": True,  # 标记：复用 K1 的 session_id
    },
    {
        "name": "K3-多轮第三轮-不带订单号-运费",
        "query": "退货运费谁出",
        "expected_refundable": None,
        "expected_verdict_any": ["ORD20260628004", "运费", "买家", "商家"],
        "banned_any": ["ORD20260628001", "ORD20260628002", "ORD20260628003"],
        "note": "【多轮会话第 3 轮】问运费，必须提到之前订单 ORD20260628004",
        "_multi_turn_followup": True,
    },
]


# =============================================================
# 工具函数
# =============================================================
def login() -> str:
    form = f"username={USERNAME}&password={PASSWORD}"
    req = urllib.request.Request(
        f"{API_BASE}/auth/login",
        data=form.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.headers.get("Set-Cookie", "").split(";")[0]
    except urllib.error.HTTPError as e:
        print(f"❌ 登录失败: {e.code} {e.read().decode()}", flush=True)
        sys.exit(1)


def chat_stream(query: str, order_no: Optional[str], cookie: str,
                session_id: Optional[str] = None) -> dict:
    body = json.dumps({
        "query": query,
        "session_id": session_id,
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
    captured_session_id = session_id
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
                captured_session_id = event.get("session_id", captured_session_id)
                break
            elif et == "error":
                answer += f"[ERROR: {event.get('message', '')}]"
                break
        return {"intent": intent, "refundable": refundable, "answer": answer,
                "session_id": captured_session_id}
    except Exception as e:
        return {"intent": None, "refundable": None, "answer": f"[REQUEST FAILED: {e}]",
                "session_id": session_id}


def check_case(case: dict, result: dict) -> tuple[bool, str]:
    answer = result["answer"]
    if "[REQUEST FAILED" in answer or "[ERROR" in answer:
        return False, f"请求/解析失败: {answer[:100]}"

    expected_refundable = case.get("expected_refundable")
    expected_verdict_any = case.get("expected_verdict_any", [])
    expected_order_no = case.get("expected_order_no")
    banned_any = case.get("banned_any", [])

    # 1. meta.refundable 必须匹配
    if expected_refundable is not None and result["refundable"] != expected_refundable:
        return False, f"meta.refundable={result['refundable']} != 期望 {expected_refundable}"

    # 2. 期望订单号必须出现（防串单）
    if expected_order_no and expected_order_no not in answer:
        return False, f"答案未包含期望订单号 {expected_order_no}"

    # 3. 期望退款结论句至少 1 个
    if expected_verdict_any and not any(k in answer for k in expected_verdict_any):
        return False, f"缺退款结论关键词 {expected_verdict_any}"

    # 4. 禁用关键词任何一个出现都 FAIL
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
    multi_turn_session_id: Optional[str] = None

    for i, case in enumerate(TEST_CASES, 1):
        # 多轮对话：seed 用例 → 复用其 session_id；followup 用例 → 用同一个 session_id
        session_id = None
        if case.get("_multi_turn_seed"):
            # seed 不传 session_id（让后端生成）
            pass
        elif case.get("_multi_turn_followup"):
            session_id = multi_turn_session_id

        print(f"\n[{i}/{len(TEST_CASES)}] {case['name']}")
        print(f"  Q: {case['query']}")
        if session_id:
            print(f"  session_id: {session_id[:12]}...（多轮复用）")
        print(f"  note: {case.get('note', '')}")
        t0 = time.time()
        result = chat_stream(case["query"], case.get("order_no"), cookie, session_id=session_id)
        elapsed = time.time() - t0

        # seed 用例：记录 session_id 供 followup 复用
        if case.get("_multi_turn_seed") and result.get("session_id"):
            multi_turn_session_id = result["session_id"]
            print(f"  → session 已 seed: {multi_turn_session_id[:12]}...")

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
                    "expected_refundable": r["case"].get("expected_refundable"),
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
