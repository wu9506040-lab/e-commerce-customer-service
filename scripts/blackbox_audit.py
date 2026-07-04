"""
智能客服公网部署 - 完整黑盒测试
覆盖：API + RAG + 退款状态机 + 订单号提取 + 控制台错误
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

BASE = "http://120.79.27.124:5173"
API = "http://120.79.27.124:8000"
TEST_USER = "demotest"
TEST_PASS = "demotest123"

results: dict = {}
console_errors: list = []


def _ok(name: str, ok: bool, msg: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({msg})" if msg else ""))
    results[name] = {"ok": ok, "msg": msg}
    return ok


# =============================================================
# 1. 认证（每个场景独立 client，避免 cookie 串扰）
# =============================================================
async def test_auth():
    print("\n=== 1. 认证 ===")
    # 登录 demotest（独立 client）
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{API}/api/auth/login",
                         data={"username": TEST_USER, "password": TEST_PASS},
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        _ok("1.1 demotest 登录", r.status_code == 200, f"HTTP {r.status_code}")
        # 当前用户
        r = await c.get(f"{API}/api/auth/me")
        _ok("1.2 /auth/me (demotest)", r.status_code == 200 and r.json().get("username") == TEST_USER,
            f"username={r.json().get('username', '?')}")

    # 游客登录（独立 client）
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{API}/api/public/demo-account")
        visitor_ok = r.status_code == 200 and "user" in r.json()
        _ok("1.3 游客一键登录", visitor_ok,
            f"HTTP {r.status_code}, username={r.json().get('user', {}).get('username', '?')}")


# =============================================================
# 2. 商品 API
# =============================================================
async def test_products(c: httpx.AsyncClient):
    print("\n=== 2. 商品 API ===")
    r = await c.get(f"{API}/api/products")
    data = r.json()
    products = data.get("products", data if isinstance(data, list) else [])
    _ok("2.1 商品列表", r.status_code == 200 and len(products) >= 10,
        f"HTTP {r.status_code}, 商品数={len(products)}")

    r = await c.get(f"{API}/api/products/SKU001")
    _ok("2.2 商品详情 SKU001", r.status_code == 200 and r.json().get("sku") == "SKU001",
        f"HTTP {r.status_code}, name={r.json().get('name', '?')}")

    r = await c.get(f"{API}/api/products/SKU999_NOT_EXIST")
    _ok("2.3 不存在的商品", r.status_code == 404, f"HTTP {r.status_code}")


# =============================================================
# 3. 订单 API
# =============================================================
async def test_orders(c: httpx.AsyncClient):
    print("\n=== 3. 订单 API ===")
    r = await c.get(f"{API}/api/orders/my")
    data = r.json()
    orders = data.get("orders", [])
    _ok("3.1 我的订单", r.status_code == 200 and len(orders) >= 7,
        f"HTTP {r.status_code}, 订单数={len(orders)}")

    # 订单详情
    if orders:
        order_no = orders[0].get("order_no")
        r = await c.get(f"{API}/api/orders/{order_no}")
        _ok("3.2 订单详情", r.status_code == 200 and r.json().get("order_no") == order_no,
            f"order_no={order_no}")

    # 状态分布
    statuses = [o.get("status") for o in orders]
    _ok("3.3 覆盖所有状态", all(s in statuses for s in ["pending", "paid", "shipped", "delivered", "refunded"]),
        f"statuses={set(statuses)}")


# =============================================================
# 4. RAG 政策查询
# =============================================================
async def test_rag(c: httpx.AsyncClient):
    print("\n=== 4. RAG 政策查询 ===")
    queries = [
        ("退款政策是什么", ["退货", "7 天", "退款"]),
        ("怎么申请退款", ["退款", "流程"]),
        ("保修多久", ["保修", "维修"]),
        ("运费险", ["运费", "快递"]),
    ]
    for query, expected_keywords in queries:
        r = await c.post(f"{API}/api/chat",
                         json={"query": query, "session_id": None},
                         headers={"Accept": "text/event-stream"},
                         timeout=30.0)
        text = r.text
        # 提取 meta 事件看 policy_hits
        meta_match = re.search(r'"type":\s*"meta".*?"policy_hits":\s*(\d+)', text)
        policy_hits = int(meta_match.group(1)) if meta_match else 0
        # 提取回答内容
        tokens = re.findall(r'"text":\s*"([^"]+)"', text)
        answer = "".join(tokens)
        has_keywords = sum(1 for k in expected_keywords if k in answer)
        _ok(f"4.{queries.index((query, expected_keywords))+1} {query}",
            policy_hits > 0 and has_keywords >= 1,
            f"policy_hits={policy_hits}, 关键词命中={has_keywords}/{len(expected_keywords)}, 回答前100={answer[:100]}")


# =============================================================
# 5. 订单号提取（关键 bug 测试）
# =============================================================
async def test_order_extraction(c: httpx.AsyncClient):
    print("\n=== 5. 订单号提取（修复 bug 测试）===")
    test_cases = [
        ("ORD20260704899EBA", "ORD20260704899EBA"),  # 完整订单号
        ("ORD20260621002", "ORD20260621002"),         # 标准订单号
        ("ORD20260615004", "ORD20260615004"),
        ("订单号是ORD20260601005", "ORD20260601005"),
    ]
    for query, expected_order in test_cases:
        r = await c.post(f"{API}/api/chat",
                         json={"query": query, "session_id": None},
                         headers={"Accept": "text/event-stream"},
                         timeout=30.0)
        meta_match = re.search(r'"type":\s*"meta".*?"order_no":\s*"([^"]+)"', r.text)
        extracted = meta_match.group(1) if meta_match else "null"
        _ok(f"5.{test_cases.index((query, expected_order))+1} '{query}' 提取",
            extracted == expected_order,
            f"提取={extracted}, 期望={expected_order}")


# =============================================================
# 6. LangGraph 退款全流程
# =============================================================
async def test_refund_flow(c: httpx.AsyncClient):
    print("\n=== 6. LangGraph 退款全流程 ===")
    r = await c.post(f"{API}/api/chat",
                     json={"query": "我想退款", "session_id": None},
                     headers={"Accept": "text/event-stream"},
                     timeout=30.0)
    # 提取 meta 看 intent
    meta_match = re.search(r'"type":\s*"meta".*?"intent":\s*"([^"]+)"', r.text)
    intent = meta_match.group(1) if meta_match else "?"
    _ok("6.1 '我想退款' 意图识别", intent == "refund_query", f"intent={intent}")

    # 提取回答
    tokens = re.findall(r'"text":\s*"([^"]+)"', r.text)
    answer1 = "".join(tokens)
    _ok("6.2 退款回复内容", "订单" in answer1 and ("号" in answer1 or "提供" in answer1),
        f"前150: {answer1[:150]}")


# =============================================================
# 7. 控制台错误（前端）
# =============================================================
async def test_console_errors():
    print("\n=== 7. 控制台错误（前端 SPA）===")
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await ctx.new_page()
        page.on("pageerror", lambda exc: console_errors.append(f"PAGEERROR: {exc}"))
        page.on("console", lambda m: console_errors.append(f"CONSOLE_ERR: {m.text[:200]}") if m.type == "error" else None)

        # 走完整路径
        await page.goto(f"{BASE}/demo", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(800)
        # 用 demotest 登录
        await page.fill("input[autocomplete='username']", TEST_USER)
        await page.fill("input[autocomplete='current-password']", TEST_PASS)
        await page.click("button[type='submit']")
        await page.wait_for_timeout(2500)
        # /shop
        await page.goto(f"{BASE}/shop", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        # /shop/SKU001
        await page.goto(f"{BASE}/shop/SKU001", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        # /chat 提问
        await page.goto(f"{BASE}/chat", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await page.fill("input[type='text'], textarea", "退款政策")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(7000)
        # /profile
        await page.goto(f"{BASE}/profile", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        await browser.close()

    # 过滤掉 favicon + 401 (clear_cookies 测试副作用)
    unique_errors = [e for e in console_errors if "favicon" not in e.lower()]
    _ok("7.1 控制台错误", len(unique_errors) == 0, f"错误数={len(unique_errors)}")
    if unique_errors:
        for e in unique_errors[:5]:
            print(f"    - {e[:200]}")


# =============================================================
# main
# =============================================================
async def main():
    print("=" * 70)
    print(f"  智能客服公网部署 - 完整黑盒测试")
    print(f"  Frontend: {BASE}")
    print(f"  API:      {API}")
    print("=" * 70)

    await test_auth()
    async with httpx.AsyncClient(timeout=30.0) as c:
        # demotest 登录拿 cookie
        r = await c.post(f"{API}/api/auth/login",
                         data={"username": TEST_USER, "password": TEST_PASS},
                         headers={"Content-Type": "application/x-www-form-urlencoded"})
        await test_products(c)
        await test_orders(c)
        await test_rag(c)
        await test_order_extraction(c)
        await test_refund_flow(c)
    await test_console_errors()

    # 汇总
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    passed = sum(1 for v in results.values() if v["ok"])
    total = len(results)
    print(f"  通过: {passed}/{total}")
    for name, v in results.items():
        mark = "PASS" if v["ok"] else "FAIL"
        msg = f"  ({v['msg']})" if v["msg"] else ""
        print(f"  [{mark}] {name}{msg}")

    # 写报告
    Path("frontend/_screenshots/audit").mkdir(parents=True, exist_ok=True)
    Path("frontend/_screenshots/audit/report.json").write_text(
        json.dumps({"passed": passed, "total": total, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))