"""看 console error 的 location/URL"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await context.new_page()

        errors = []
        def on_console(msg):
            if msg.type == "error":
                loc = msg.location
                errors.append(f"{msg.text} | {loc.get('url','')}:{loc.get('lineNumber','')}")

        page.on("console", on_console)

        # 跑一遍主要页面
        await page.goto("http://localhost:5173/", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1500)

        # 登录
        await page.goto("http://localhost:5173/login", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("input[autocomplete='username']", timeout=10000)
        await page.fill("input[autocomplete='username']", "demotest")
        await page.fill("input[autocomplete='current-password']", "demotest123")
        await page.click("button[type='submit']")
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        await page.wait_for_timeout(1000)

        # chat 页
        await page.goto("http://localhost:5173/chat", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # 发个消息
        await page.fill("input[type='text'], textarea", "你好")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(5000)

        # 转人工
        await page.fill("input[type='text'], textarea", "我要转人工")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(5000)

        print(f"Console errors: {len(errors)}")
        for e in errors:
            print(f"  {e}")
        await browser.close()

asyncio.run(main())