"""
追踪控制台 404 错误的来源
"""
import asyncio
from playwright.async_api import async_playwright

BASE = "http://120.79.27.124:5173"


async def main():
    errors = []
    failed_requests = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await ctx.new_page()
        page.on("pageerror", lambda exc: errors.append(f"PAGEERROR: {exc}"))
        page.on("console", lambda m: errors.append(f"CONSOLE[{m.type}]: {m.text[:200]}") if m.type == "error" else None)
        page.on("requestfailed", lambda req: failed_requests.append(f"{req.method} {req.url} - {req.failure}"))
        page.on("response", lambda r: failed_requests.append(f"{r.status} {r.url}") if r.status >= 400 else None)

        # 完整路径
        await page.goto(f"{BASE}/demo", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        await page.fill("input[autocomplete='username']", "demotest")
        await page.fill("input[autocomplete='current-password']", "demotest123")
        await page.click("button[type='submit']")
        await page.wait_for_timeout(3000)
        await page.goto(f"{BASE}/shop/SKU001", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await page.goto(f"{BASE}/chat", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await page.fill("input[type='text'], textarea", "退款政策")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(7000)
        await page.goto(f"{BASE}/profile", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        await browser.close()

    print("=== 失败的请求 ===")
    for r in failed_requests:
        print(f"  {r}")
    print(f"\n=== console 错误 ===")
    for e in errors:
        print(f"  {e}")


asyncio.run(main())