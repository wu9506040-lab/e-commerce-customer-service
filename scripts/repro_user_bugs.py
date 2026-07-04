"""
复现用户报告的所有问题
按用户的原话顺序：
  1. 游客登录没商品
  2. 无法退出登录
  3. 商品页看不到商品
  4. 没看到智能客服入口
  5. 游客登录后点登录注册没反应
  6. 死循环（退不出来，等不进去）

每个场景都截图 + 抓 console error + 给出可能根因
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Page

BASE = "http://120.79.27.124:5173"
OUT = Path(__file__).parent.parent / "frontend" / "_screenshots" / "bugrepro"
OUT.mkdir(parents=True, exist_ok=True)


async def shot(page: Page, name: str, label: str = ""):
    path = OUT / f"{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    print(f"  Screenshot: {path.name}  {label}")


async def bug1_no_products_for_visitor(page: Page):
    """Bug #1: 游客登录后 /shop 没商品"""
    print("\n=== Bug #1: 游客登录后 /shop 是否显示商品 ===")
    await page.context.clear_cookies()
    # 走一键 demo 登录
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(1000)
    await shot(page, "01a-login-page", "登录页")

    demo_btn = await page.query_selector(".btn-demo")
    if demo_btn:
        await demo_btn.click()
        await page.wait_for_timeout(3000)
        print(f"  当前 URL: {page.url}")
        await shot(page, "01b-after-demo-login", "一键 demo 登录后")

    # 访问 /shop
    await page.goto(f"{BASE}/shop", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    print(f"  当前 URL: {page.url}")
    content = await page.content()
    has_sku = "SKU001" in content or "ZP1" in content or "智选" in content
    product_count = content.count("product-card") + content.count("点击查看")
    print(f"  含 SKU 关键词: {has_sku}, product-card 数量: {product_count}")
    await shot(page, "01c-shop-page", "游客视角的 /shop")
    return has_sku, product_count


async def bug2_logout_button(page: Page):
    """Bug #2: 无法退出登录"""
    print("\n=== Bug #2: 退出登录按钮 ===")
    # 先确保已登录
    await page.context.clear_cookies()
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    demo_btn = await page.query_selector(".btn-demo")
    if demo_btn:
        await demo_btn.click()
        await page.wait_for_timeout(3000)

    # 去 /profile
    await page.goto(f"{BASE}/profile", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    print(f"  当前 URL: {page.url}")
    await shot(page, "02a-profile-page", "个人中心")

    # 找退出登录按钮
    logout_btn = await page.query_selector("button:has-text('退出'), a:has-text('退出'), .logout-btn")
    print(f"  找到退出登录按钮: {logout_btn is not None}")
    if logout_btn:
        text = await logout_btn.inner_text()
        print(f"  按钮文本: '{text.strip()}'")
        await logout_btn.click()
        await page.wait_for_timeout(2000)
        print(f"  点击后 URL: {page.url}")
        await shot(page, "02b-after-logout-click", "点退出后")
    return logout_btn is not None


async def bug3_product_detail_blank(page: Page):
    """Bug #3: 商品页看不到商品"""
    print("\n=== Bug #3: 商品详情页 /shop/SKU001 ===")
    await page.context.clear_cookies()
    # 用 demotest 登录（有订单的用户）
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    await page.fill("input[autocomplete='username']", "demotest")
    await page.fill("input[autocomplete='current-password']", "demotest123")
    await page.click("button[type='submit']")
    await page.wait_for_timeout(3000)
    print(f"  demotest 登录后 URL: {page.url}")

    # 访问商品详情
    await page.goto(f"{BASE}/shop/SKU001", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    print(f"  当前 URL: {page.url}")
    content = await page.content()
    has_product_info = any(k in content for k in ["ZP1", "5999", "SKU001", "智选科技"])
    print(f"  含商品信息关键词: {has_product_info}")
    # 看页面 body 文本长度（如果极短说明空白）
    body_text = await page.evaluate("() => document.body.innerText")
    print(f"  页面文本长度: {len(body_text)} chars")
    print(f"  文本前 200: {body_text[:200]}")
    await shot(page, "03-product-detail", "商品详情页")
    return has_product_info, len(body_text)


async def bug4_no_chat_entry(page: Page):
    """Bug #4: 没看到智能客服入口"""
    print("\n=== Bug #4: 智能客服入口 ===")
    # 在商品详情页找"咨询客服"按钮
    await page.goto(f"{BASE}/shop/SKU001", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    ask_btns = await page.query_selector_all("button")
    ask_btns_text = []
    for btn in ask_btns:
        try:
            t = (await btn.inner_text()).strip()
            if t and any(k in t for k in ["咨询", "客服", "chat", "Chat"]):
                ask_btns_text.append(t)
        except Exception:
            pass
    print(f"  含'咨询/客服/chat'的按钮: {ask_btns_text}")

    # 在 /profile 找 chat 入口
    await page.goto(f"{BASE}/profile", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    chat_links = await page.query_selector_all("a[href*='/chat']")
    print(f"  /profile 中 /chat 链接数: {len(chat_links)}")

    # 在 /shop 找 chat 入口
    await page.goto(f"{BASE}/shop", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    chat_links_shop = await page.query_selector_all("a[href*='/chat']")
    print(f"  /shop 中 /chat 链接数: {len(chat_links_shop)}")
    await shot(page, "04-shop-page", "商城页找 chat 入口")
    return len(ask_btns_text), len(chat_links), len(chat_links_shop)


async def bug5_login_after_loggedin(page: Page):
    """Bug #5: 游客登陆后点登录注册没反应"""
    print("\n=== Bug #5: 已登录状态下访问 /login ===")
    # 游客登录
    await page.context.clear_cookies()
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    demo_btn = await page.query_selector(".btn-demo")
    if demo_btn:
        await demo_btn.click()
        await page.wait_for_timeout(3000)

    # 已登录后再访问 /login
    resp = await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    print(f"  已登录访问 /login → HTTP {resp.status if resp else 'N/A'}, URL: {page.url}")
    content = await page.content()
    body_text = await page.evaluate("() => document.body.innerText")
    print(f"  页面文本长度: {len(body_text)}")
    print(f"  文本前 200: {body_text[:200]}")
    await shot(page, "05-logged-in-visit-login", "已登录访问 /login")
    return resp.status if resp else None, len(body_text)


async def main():
    print("=" * 70)
    print(" 用户报告的 Bug 复现 - 公网部署 http://120.79.27.124:5173")
    print("=" * 70)

    console_errors: list = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            ignore_https_errors=True,
        )
        page = await context.new_page()

        page.on("pageerror", lambda exc: console_errors.append(f"PAGEERROR: {exc}"))
        page.on("console", lambda m: console_errors.append(f"CONSOLE[{m.type}]: {m.text[:200]}") if m.type == "error" else None)

        results = {}
        results["bug1_shop_for_visitor"] = await bug1_no_products_for_visitor(page)
        results["bug2_logout"] = await bug2_logout_button(page)
        results["bug3_product_detail"] = await bug3_product_detail_blank(page)
        results["bug4_chat_entry"] = await bug4_no_chat_entry(page)
        results["bug5_login_when_logged_in"] = await bug5_login_after_loggedin(page)

        await browser.close()

    print("\n" + "=" * 70)
    print(" 复现结果汇总")
    print("=" * 70)
    for k, v in results.items():
        print(f"  {k}: {v}")
    print(f"\n  控制台错误总数: {len([e for e in console_errors if 'favicon' not in e.lower()])}")
    for e in console_errors[:20]:
        if "favicon" not in e.lower():
            print(f"    - {e[:200]}")
    print(f"\n  截图: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))