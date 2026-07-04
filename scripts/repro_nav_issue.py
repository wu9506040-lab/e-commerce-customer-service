"""
复现用户的新问题：登录后点"登录注册"一直停在 /shop，找不到退出入口
"""
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://120.79.27.124:5173"
OUT = Path(__file__).parent.parent / "frontend" / "_screenshots" / "bugrepro"
OUT.mkdir(parents=True, exist_ok=True)


async def main():
    print("=" * 70)
    print(" 用户路径复现：登录后 → 点登录注册")
    print("=" * 70)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await ctx.new_page()

        # 1. 一键 demo 登录
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        await page.click(".btn-demo")
        await page.wait_for_timeout(3000)
        print(f"\n[1] demo 登录后 URL: {page.url}")
        await page.screenshot(path=str(OUT / "10-after-demo-login.png"), full_page=True)

        # 2. 找顶部导航所有链接和按钮
        print(f"\n[2] 顶部 nav 元素：")
        nav_items = await page.query_selector_all("nav a, nav button, header a, header button, .nav-item, .nav a")
        for i, item in enumerate(nav_items[:20]):
            try:
                t = (await item.inner_text()).strip()
                href = await item.get_attribute("href") or ""
                if t or href:
                    print(f"  {i:2d}. '{t}'  href={href}")
            except Exception:
                pass

        # 3. 模拟用户操作：点"登录注册"链接
        print(f"\n[3] 点 '登录注册' 链接")
        login_reg_link = None
        for sel in ["text=登录注册", "a:has-text('登录')", "a:has-text('注册')"]:
            login_reg_link = await page.query_selector(sel)
            if login_reg_link:
                print(f"  找到: {sel}")
                break

        if login_reg_link:
            before_url = page.url
            await login_reg_link.click()
            await page.wait_for_timeout(3000)
            after_url = page.url
            print(f"  点击前 URL: {before_url}")
            print(f"  点击后 URL: {after_url}")
            print(f"  是否变化: {before_url != after_url}")
            await page.screenshot(path=str(OUT / "11-after-click-login-reg.png"), full_page=True)

            if before_url == after_url:
                print(f"\n[!!! 复现成功] URL 没变，找不到登录入口")
        else:
            print(f"  没找到任何'登录注册'链接")

        # 4. 用户可能想"切换账号"或者"退出登录" - 看 nav 有没有这些入口
        print(f"\n[4] 找 '切换账号' / '退出登录' / 'Switch' 入口")
        for sel in ["text=退出登录", "text=退出", "text=切换账号", "text=Switch Account", ".logout-btn", "[data-test='logout']"]:
            elem = await page.query_selector(sel)
            print(f"  {sel}: {'找到' if elem else '未找到'}")

        # 5. 用户实际能点到的所有链接
        print(f"\n[5] 当前页面（{page.url}）所有可点击的 a/button：")
        all_clicks = await page.query_selector_all("a, button")
        for i, c in enumerate(all_clicks[:30]):
            try:
                t = (await c.inner_text()).strip()
                tag = await c.evaluate("e => e.tagName.toLowerCase()")
                href = await c.get_attribute("href") or ""
                if t:
                    print(f"  {i:2d}. <{tag}> '{t[:30]}'  href={href[:50]}")
            except Exception:
                pass

        # 6. 看一下 /profile 是否有退出按钮
        await page.goto(f"{BASE}/profile", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        print(f"\n[6] /profile 页面退出按钮：")
        logout = await page.query_selector(".logout-btn, button:has-text('退出')")
        if logout:
            print(f"  找到: {(await logout.inner_text()).strip()}")
            visible = await logout.is_visible()
            print(f"  可见: {visible}")
        else:
            print(f"  未找到")
        await page.screenshot(path=str(OUT / "12-profile-page.png"), full_page=True)

        await browser.close()


asyncio.run(main())