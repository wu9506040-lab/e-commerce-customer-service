"""模拟 verify_demo_public.py 多步骤，看具体哪个资源 404"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN", ignore_https_errors=True)
        page = await context.new_page()

        failed = []
        def on_response(resp):
            if resp.status >= 400 and "/api/" not in resp.url:
                failed.append(f"{resp.status} {resp.url}")
        page.on("response", on_response)

        # step 1
        await page.goto("http://localhost:5173/", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1500)

        # step 2: 演示登录
        await page.goto("http://localhost:5173/login?tab=login", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector(".btn-demo", timeout=10000)
        await page.click(".btn-demo")
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        await page.wait_for_timeout(2500)

        # step 5: shop
        await page.goto("http://localhost:5173/shop", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # step 6: chat
        await page.goto("http://localhost:5173/chat", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # step 8: profile
        await page.goto("http://localhost:5173/profile", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # step 9: chat + 转人工
        await page.goto("http://localhost:5173/chat", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)
        await page.fill("input[type='text'], textarea", "我要转人工")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(8000)

        print(f"4xx/5xx (excluding /api): {len(failed)}")
        for f in failed:
            print(f"  {f}")
        await browser.close()

asyncio.run(main())