"""
验证：已登录访问 /login 的新体验
应该看到：顶部条 + "您已登录为 xxx" + "切换账号"按钮
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://120.79.27.124:5173"
OUT = Path(__file__).parent.parent / "frontend" / "_screenshots" / "bugrepro"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await ctx.new_page()

        # 1. 一键 demo 登录
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        await page.click(".btn-demo")
        await page.wait_for_timeout(3000)
        print(f"[1] demo 登录后 URL: {page.url}")

        # 2. 已登录访问 /login
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        print(f"[2] 已登录访问 /login → URL: {page.url}")
        body_text = await page.evaluate("() => document.body.innerText")
        print(f"    页面文本前 300: {body_text[:300]}")
        await page.screenshot(path=str(OUT / "20-loggedin-login-page.png"), full_page=True)

        # 3. 找"切换账号"按钮
        switch_btn = await page.query_selector(".btn-switch")
        print(f"[3] '切换账号' 按钮: {'找到' if switch_btn else '未找到'}")

        # 4. 点"切换账号"按钮 → 应该回到未登录状态
        if switch_btn:
            await switch_btn.click()
            await page.wait_for_timeout(3000)
            print(f"[4] 点切换账号后 URL: {page.url}")
            body_text = await page.evaluate("() => document.body.innerText")
            print(f"    页面文本前 200: {body_text[:200]}")
            await page.screenshot(path=str(OUT / "21-after-switch-account.png"), full_page=True)

        await browser.close()


asyncio.run(main())