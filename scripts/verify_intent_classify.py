"""
4 意图分类基准 + 边界测试 — P0 核心 AI 链路
================================================

覆盖 4 类意图 × 6 用例 = 24 + 1 跨意图 = 25 条：

| 意图           | 基准 4                       | 边界 2                         |
|----------------|------------------------------|--------------------------------|
| order_query    | 我的订单状态 / ORD..到哪 / 物流 / 我的那笔ZP1 | 口语"东西到哪了" / 错别字"发壱到哪" |
| refund_query   | 我想退款 / 怎么退 / 退货 / ORD..退一下 | 想退掉 / 给退下不 |
| product_query  | ZP1 多少钱 / BP1 库存 / ZP1 续航 / ZP1 规格 | 推荐个手机 / ZP1 有货吗 |
| policy_query   | 7天无理由 / 保修多久 / 运费险 / 包邮吗 | 怎么申请退款（回归）/ 7天无理由退货运费 |

跨意图边界 1：
    "我的订单 ORD20260704004 怎么保修？" → product_query 还是 policy_query？
    （按当前实现应走 policy_query，与 order 无关）

验证方式：解析 SSE meta.intent 字段
- 用 demotest 登录
- 公共查询可走；订单/退款需登录态

运行：
    python scripts/verify_intent_classify.py
"""
import asyncio
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx

BASE = "http://120.79.27.124:8000"
USERNAME = "demotest"
PASSWORD = "demotest123"
REPORT_PATH = Path(__file__).parent.parent / "frontend/_screenshots/intent_classify_report.json"

results: dict = {}


# =============================================================
# 工具
# =============================================================
def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith("data: ")]


def _first_meta(events: list[dict]) -> Optional[dict]:
    return next((e for e in events if e.get("type") == "meta"), None)


async def login(client: httpx.AsyncClient) -> bool:
    form = urllib.parse.urlencode({"username": USERNAME, "password": PASSWORD})
    r = await client.post(
        f"{BASE}/api/auth/login", content=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return r.status_code == 200


async def classify(client: httpx.AsyncClient, query: str) -> dict:
    """POST /chat，返 {intent, method, entities}"""
    r = await client.post(
        f"{BASE}/api/chat",
        json={"query": query, "session_id": None},
        headers={"Accept": "text/event-stream"},
        timeout=30.0,
    )
    if r.status_code != 200:
        return {"http_status": r.status_code, "error": r.text[:200]}
    meta = _first_meta(_parse_sse(r.text))
    return {
        "http_status": 200,
        "intent": meta.get("intent") if meta else None,
        "method": meta.get("intent_method") if meta else None,
        "entities": meta.get("entities") if meta else {},
    }


def _ok(name: str, ok: bool, msg: str = ""):
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}" + (f"  ({msg})" if msg else "")
    print(line)
    results[name] = {"ok": ok, "msg": msg}
    return ok


async def run_one(client: httpx.AsyncClient, label: str, query: str, expected_intent: str,
                  bounds: str = "") -> bool:
    """跑单条用例：发 query → 验 intent"""
    r = await classify(client, query)
    actual = r.get("intent")
    method = r.get("method", "?")
    ok = actual == expected_intent
    suffix = f" [{bounds}]" if bounds else ""
    msg = f"query={query!r}, intent={actual} (期望 {expected_intent}), method={method}"
    return _ok(f"{label}{suffix}", ok, msg)


# =============================================================
# 4 类意图用例表
# =============================================================
ORDER_BASE = [
    ("order_query 基准·状态", "我的订单状态"),
    ("order_query 基准·ORD到哪", "ORD20260704004 到哪了"),
    ("order_query 基准·物流", "查一下物流"),
    ("order_query 基准·我的那笔", "我的那笔 ZP1 订单"),
]
ORDER_BOUND = [
    ("order_query 边界·口语",  "东西到哪了"),                     # 历史可能的 bug：用"东西"代指订单
    ("order_query 边界·错别字", "我哩订单啥状态"),                  # 同义错别字
]

REFUND_BASE = [
    ("refund_query 基准·我想退款", "我想退款"),
    ("refund_query 基准·怎么退",   "怎么退"),                       # 该走 refund 还是 policy? 当前看应 refund
    ("refund_query 基准·退货",     "我要退货"),                     # 含个人意愿 + 退货
    ("refund_query 基准·ORD退一下", "ORD20260704004 退一下"),
]
REFUND_BOUND = [
    ("refund_query 边界·想退掉",  "想退掉那个耳机"),
    ("refund_query 边界·给退不",  "这件给退不"),
]

PRODUCT_BASE = [
    ("product_query 基准·多少钱", "ZP1 多少钱"),
    ("product_query 基准·库存",   "BP1 有库存吗"),                 # 注意"有"是规则关键词吗？看 code
    ("product_query 基准·续航",   "ZP1 续航怎么样"),
    ("product_query 基准·规格",   "ZP1 规格参数"),
]
PRODUCT_BOUND = [
    ("product_query 边界·推荐", "推荐个手机"),
    ("product_query 边界·有货吗", "ZP1 有货吗"),
]

POLICY_BASE = [
    ("policy_query 基准·7天无理由", "7 天无理由"),
    ("policy_query 基准·保修多久",   "保修多久"),
    ("policy_query 基准·运费险",     "运费险"),                       # 注意：运费险可能是 product_query，看实际表现
    ("policy_query 基准·包邮",       "包邮吗"),
]
POLICY_BOUND = [
    ("policy_query 边界·流程（回归 bug）", "怎么申请退款"),           # M13 历史 bug 修复点
    ("policy_query 边界·运费",   "7 天无理由退货运费谁出"),
]

CROSS_INTENT = [
    # "我的订单 ORD..怎么保修" — 含具体订单号 + 问保修
    # 产品决定：先按 order_query 拉订单上下文，再走 product_query/policy_query 解释
    # 当前实现 → order_query 先匹配（"我的订单"），业务可接受
    # 用例期望：order_query 或 policy_query 均可（看实现）
    ("跨意图·订单+保修", "我的订单 ORD20260704004 怎么保修", ("order_query", "policy_query")),
]


async def main():
    print("=" * 70)
    print(f"  4 意图分类基准 + 边界测试 — {BASE}")
    print("=" * 70)

    async with httpx.AsyncClient(timeout=30.0) as client:
        if not await login(client):
            print("  [FAIL] demotest 登录")
            return 1
        print("  [PASS] demotest 登录\n")

        # order
        print("--- order_query ---")
        for label, q in ORDER_BASE:
            await run_one(client, label, q, "order_query", "基准")
        for label, q in ORDER_BOUND:
            await run_one(client, label, q, "order_query", "边界")

        # refund
        print("\n--- refund_query ---")
        for label, q in REFUND_BASE:
            await run_one(client, label, q, "refund_query", "基准")
        for label, q in REFUND_BOUND:
            await run_one(client, label, q, "refund_query", "边界")

        # product
        print("\n--- product_query ---")
        for label, q in PRODUCT_BASE:
            await run_one(client, label, q, "product_query", "基准")
        for label, q in PRODUCT_BOUND:
            await run_one(client, label, q, "product_query", "边界")

        # policy
        print("\n--- policy_query ---")
        for label, q in POLICY_BASE:
            await run_one(client, label, q, "policy_query", "基准")
        for label, q in POLICY_BOUND:
            await run_one(client, label, q, "policy_query", "边界")

        # cross intent（接受多意图命中）
        print("\n--- 跨意图边界 ---")
        for label, q, expected_set in CROSS_INTENT:
            r = await classify(client, q)
            actual = r.get("intent")
            ok = actual in expected_set
            _ok(
                label,
                ok,
                f"query={q!r}, intent={actual} (期望 {expected_set})",
            )

    # 汇总
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    passed = sum(1 for v in results.values() if v["ok"])
    total = len(results)
    print(f"  通过: {passed}/{total}")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps({"passed": passed, "total": total, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  报告: {REPORT_PATH}")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
