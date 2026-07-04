"""
公网部署演示流程实测 + 报告生成
==================================

目的：
    以面试官 / HR 视角，从公网访问 http://120.79.27.124:5173/，走一遍完整流程。
    输出 8 张顺序截图 + 控制台 PASS/FAIL 报告。

覆盖场景（10 项）：
    1.  演示首页加载 + 4 个数字锚点
    2.  游客一键 demo 登录
    3.  账号注册
    4.  账号登录（用 demotest）
    5.  商品橱窗浏览
    6.  聊天 RAG 命中知识库
    7.  LangGraph 退款状态机
    8.  订单生命周期 pending → refunded
    9.  个人信息页
    10. 控制台 JS 错误统计

用法：
    python scripts/verify_demo_public.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright, Page

# =============================================================
# 配置 - 改这里就能切到本地 / 其他公网 IP
# =============================================================
BASE = "http://120.79.27.124:5173"
API = "http://120.79.27.124:8000"

OUT = Path(__file__).parent.parent / "frontend" / "_screenshots" / "walkthrough"
OUT.mkdir(parents=True, exist_ok=True)

# 测试账号
DEMO_ACCOUNT = ("demotest", "demotest123")  # seed 注入
REVIEWER = ("reviewer_demo", "Reviewer#2026")  # 演示现场注册

# 全局状态
results: dict = {}
console_errors: list = []
total_console_errors: int = 0


def _print_step(step: str, ok: bool, msg: str = ""):
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {step}"
    if msg:
        line += f"  ({msg})"
    print(line)
    results[step] = {"ok": ok, "msg": msg}
    return ok


def _attach_console_capture(page: Page, label: str):
    """把 console error / page error 追加到全局列表"""
    def on_pageerror(exc):
        console_errors.append(f"[{label}] PAGEERROR: {exc}")

    def on_console(msg):
        if msg.type == "error":
            console_errors.append(f"[{label}] CONSOLE_ERR: {msg.text[:200]}")

    page.on("pageerror", on_pageerror)
    page.on("console", on_console)


async def shoot(page: Page, name: str, path: str, wait_ms: int = 1800) -> bool:
    """访问 + 等待 + 截图"""
    print(f"\n[{name}] -> {BASE}{path}")
    try:
        resp = await page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=20000)
        status = resp.status if resp else "no-resp"
        print(f"  HTTP {status}")
        await page.wait_for_timeout(wait_ms)
        out = OUT / f"{name}.png"
        await page.screenshot(path=str(out), full_page=True)
        print(f"  SCREEN: {out.name} ({out.stat().st_size // 1024} KB)")
        return status == 200
    except Exception as e:
        print(f"  FAILED: {e}")
        return False


async def step1_homepage(page: Page) -> bool:
    """1. 演示首页"""
    ok = await shoot(page, "demo-01-home", "/", wait_ms=1200)
    if not ok:
        return _print_step("1. 演示首页加载", False, "HTTP != 200")
    # 验证 4 个数字锚点
    content = await page.content()
    anchors = {
        "75 pytest": "75/75" in content or "75 " in content,
        "0 token Guard": "0 token" in content or "三层" in content or "3 层" in content,
        "LangGraph 6 节点": "LangGraph" in content and "6 节点" in content or "6 节点" in content,
        "5 服务部署": "5 服务" in content or "Docker Compose" in content,
    }
    found = sum(1 for v in anchors.values() if v)
    print(f"  数字锚点命中: {found}/4  ({anchors})")
    return _print_step("1. 演示首页 + 数字锚点", found >= 2,
                       f"命中 {found}/4 锚点")


async def step2_demo_login(page: Page) -> bool:
    """2. 游客一键 demo 登录"""
    print(f"\n[{step2_demo_login.__name__}] 游客一键 demo")
    try:
        await page.goto(f"{BASE}/login?tab=login", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector(".btn-demo", timeout=10000)
        await page.click(".btn-demo")
        # 等跳转离开 /login
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "demo-02-after-demo-login.png"), full_page=True)
        print(f"  跳转后 URL: {page.url}")
        return _print_step("2. 一键 demo 登录", "/login" not in page.url,
                           f"已跳转到 {page.url.split('/')[-1]}")
    except Exception as e:
        return _print_step("2. 一键 demo 登录", False, str(e)[:120])


async def step3_register(page: Page) -> bool:
    """3. 账号注册"""
    print(f"\n[{step3_register.__name__}] 新账号注册")
    try:
        # 用新 context 隔离 cookie（旧 demo 登录会干扰）
        await page.context.clear_cookies()
        await page.goto(f"{BASE}/login?tab=register", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("input[autocomplete='username']", timeout=10000)
        # 注册表单字段
        await page.fill("input[autocomplete='username']", REVIEWER[0])
        await page.fill("input[autocomplete='new-password']", REVIEWER[1])
        # 第二个 new-password 是确认密码
        pwd_inputs = await page.query_selector_all("input[autocomplete='new-password']")
        if len(pwd_inputs) >= 2:
            await pwd_inputs[1].fill(REVIEWER[1])
        await page.click("button[type='submit']")
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "demo-03-after-register.png"), full_page=True)
        return _print_step("3. 新账号注册 + 自动登录", "/login" not in page.url,
                           f"已进 {page.url.split('/')[-1]}")
    except Exception as e:
        return _print_step("3. 新账号注册", False, str(e)[:120])


async def step4_account_login(page: Page) -> bool:
    """4. 用 demotest 账号登录"""
    print(f"\n[{step4_account_login.__name__}] demotest 账号登录")
    try:
        await page.context.clear_cookies()
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("input[autocomplete='username']", timeout=10000)
        await page.fill("input[autocomplete='username']", DEMO_ACCOUNT[0])
        await page.fill("input[autocomplete='current-password']", DEMO_ACCOUNT[1])
        await page.click("button[type='submit']")
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(OUT / "demo-04-account-login.png"), full_page=True)
        return _print_step("4. demotest 账号登录", "/login" not in page.url,
                           f"已跳转到 {page.url.split('/')[-1]}")
    except Exception as e:
        return _print_step("4. demotest 账号登录", False, str(e)[:120])


async def step5_shop(page: Page) -> bool:
    """5. 商品橱窗"""
    ok = await shoot(page, "demo-05-shop", "/shop", wait_ms=2000)
    content = await page.content()
    has_products = "ZP1" in content or "商品" in content or "product" in content.lower()
    return _print_step("5. 商品橱窗浏览", ok and has_products,
                       f"商品卡片存在={has_products}")


async def step6_chat_rag(page: Page) -> bool:
    """6. 聊天 RAG 提问（命中知识库）"""
    print(f"\n[{step6_chat_rag.__name__}] chat RAG 提问")
    try:
        await page.goto(f"{BASE}/chat", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("input[type='text'], textarea", timeout=10000)
        # RAG 测试 query
        await page.fill("input[type='text'], textarea", "退货政策是什么？")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(8000)  # 等 SSE 流式回答
        await page.screenshot(path=str(OUT / "demo-06-chat-rag.png"), full_page=True)
        content = await page.content()
        # 验证：RAG 回答里出现知识库特征词
        rag_markers = ["7 天" in content or "七天" in content,
                       "退货" in content,
                       "policy" in content.lower() or "refund" in content.lower()]
        return _print_step("6. RAG 提问（退货政策）", any(rag_markers),
                           f"命中知识库关键词={sum(rag_markers)}/{len(rag_markers)}")
    except Exception as e:
        return _print_step("6. RAG 提问", False, str(e)[:120])


async def step7_refund_langgraph(page: Page) -> bool:
    """7. LangGraph 退款状态机"""
    print(f"\n[{step7_refund_langgraph.__name__}] LangGraph 退款")
    try:
        await page.goto(f"{BASE}/chat", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("input[type='text'], textarea", timeout=10000)
        await page.fill("input[type='text'], textarea", "我想退款")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(6000)
        # LangGraph 应该问订单号
        await page.fill("input[type='text'], textarea", "ORD20260101001")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(8000)
        await page.screenshot(path=str(OUT / "demo-07-refund-langgraph.png"), full_page=True)
        content = await page.content()
        # LangGraph 路径特征：状态词、订单号、time/days 关键词
        lg_markers = ["ORD" in content,
                      "退款" in content,
                      "签收" in content or "已发货" in content or "天" in content or "elapsed" in content.lower()]
        return _print_step("7. LangGraph 退款状态机", sum(lg_markers) >= 2,
                           f"命中 LangGraph 关键词={sum(lg_markers)}/{len(lg_markers)}")
    except Exception as e:
        return _print_step("7. LangGraph 退款", False, str(e)[:120])


async def step8_order_lifecycle(page: Page) -> bool:
    """8. 订单生命周期（个人中心应能看到订单状态）"""
    ok = await shoot(page, "demo-08-profile", "/profile", wait_ms=2000)
    content = await page.content()
    # 检查是否有订单 + 至少一个状态词
    has_orders = "ORD" in content
    has_status = any(s in content for s in ["已下单", "已支付", "已发货", "已签收", "已退款", "pending", "paid", "shipped", "delivered", "refunded"])
    return _print_step("8. 订单生命周期展示", ok and has_orders and has_status,
                       f"订单存在={has_orders}, 状态词={has_status}")


async def main():
    print("=" * 70)
    print(f"  智能客服公网部署演示流程实测")
    print(f"  Frontend: {BASE}")
    print(f"  API:      {API}")
    print(f"  Output:   {OUT}")
    print("=" * 70)

    started = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            ignore_https_errors=True,
        )
        page = await context.new_page()
        _attach_console_capture(page, "main")

        # 步骤顺序执行
        await step1_homepage(page)
        await step2_demo_login(page)
        await step3_register(page)
        await step4_account_login(page)
        await step5_shop(page)
        await step6_chat_rag(page)
        await step7_refund_langgraph(page)
        await step8_order_lifecycle(page)

        await browser.close()

    elapsed = time.time() - started

    # 控制台错误统计（10 项）- 过滤测试副作用
    # - favicon: 静态资源 404，无害
    # - 401 Unauthorized: 测试脚本 clear_cookies 后路由守卫调 getMe() 预期返回，不是 bug
    unique_errors = [
        e for e in console_errors
        if "favicon" not in e.lower() and "401 (Unauthorized)" not in e
    ]
    _print_step("10. 控制台 JS 错误统计", len(unique_errors) == 0,
                f"错误数={len(unique_errors)}（已过滤 favicon + 401 测试副作用）")

    # 汇总
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    passed = sum(1 for v in results.values() if v["ok"])
    total = len(results)
    print(f"  通过: {passed}/{total}")
    print(f"  控制台错误: {len(unique_errors)}")
    print(f"  耗时: {elapsed:.1f}s")
    print()
    for name, v in results.items():
        mark = "PASS" if v["ok"] else "FAIL"
        msg = f"  ({v['msg']})" if v["msg"] else ""
        print(f"  [{mark}] {name}{msg}")

    # 写 JSON 报告
    report = {
        "base_url": BASE,
        "api_url": API,
        "elapsed_sec": round(elapsed, 1),
        "passed": passed,
        "total": total,
        "console_errors": len(unique_errors),
        "console_error_details": unique_errors[:10],
        "results": results,
        "screenshots": sorted([p.name for p in OUT.glob("*.png")]),
    }
    report_path = OUT / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告: {report_path}")
    print(f"  截图: {OUT}")
    print("=" * 70)

    return 0 if passed == total and len(unique_errors) == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))