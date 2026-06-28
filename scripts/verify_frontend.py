"""
M9.5 京东红前端视觉验证
- 自动登录（用 convtest 账号或新注册）
- 截图：首页 / 登录 / 商城 / 详情 / 聊天 / 个人中心
- 控制台错误捕获（红色 = 有问题）
- 输出到 frontend/_screenshots/
"""
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://localhost:5173"
API = "http://localhost:8000"
OUT = Path(__file__).parent.parent / "frontend" / "_screenshots"
OUT.mkdir(parents=True, exist_ok=True)

# 验证页清单
PAGES = [
    ("demo",  "/demo",  "演示首页"),
    ("login", "/login", "登录注册"),
    ("shop",  "/shop",  "商品橱窗"),
]

# 需登录才能看的
AUTH_PAGES = [
    ("chat",    "/chat",                   "对话页（未登录会跳登录）"),
    ("profile", "/profile",                "个人中心"),
]


async def login(page) -> bool:
    """用 demotest 测试账号登录（脚本注入的演示数据）"""
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector("input[autocomplete='username']", timeout=10000)
    await page.fill("input[autocomplete='username']", "demotest")
    await page.fill("input[autocomplete='current-password']", "demotest123")
    await page.click("button[type='submit']")
    # 等跳转
    try:
        await page.wait_for_url(lambda url: "/login" not in url, timeout=10000)
        return True
    except Exception:
        return False


async def shoot(page, name: str, path: str, wait_ms: int = 1500):
    """访问 + 等待 + 截图 + 收集 console error"""
    errors = []
    page.on("pageerror", lambda exc: errors.append(f"PAGEERROR: {exc}"))
    page.on("console", lambda msg: errors.append(f"CONSOLE[{msg.type}]: {msg.text}") if msg.type == "error" else None)

    print(f"\n[{name}] -> {BASE}{path}")
    try:
        resp = await page.goto(f"{BASE}{path}", wait_until="domcontentloaded", timeout=15000)
        if resp is None:
            print(f"  WARN: no response")
        else:
            print(f"  HTTP {resp.status}")
        # 额外等待 JS + 数据 fetch
        await page.wait_for_timeout(wait_ms)
        out = OUT / f"{name}.png"
        await page.screenshot(path=str(out), full_page=True)
        print(f"  SCREEN: {out.name} ({out.stat().st_size // 1024} KB)")
    except Exception as e:
        print(f"  FAILED: {e}")
        errors.append(f"NAV: {e}")

    if errors:
        print(f"  {len(errors)} error(s):")
        for e in errors[:5]:
            print(f"    - {str(e)[:160]}")
    else:
        print(f"  no errors")
    return errors


async def main():
    print(f"=== M9.5 Frontend Visual Verification ===")
    print(f"Frontend: {BASE}")
    print(f"Output:   {OUT}\n")

    all_errors = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # 1) 未登录页面
        for name, path, label in PAGES:
            errs = await shoot(page, name, path)
            all_errors[name] = errs

        # 2) 登录后页面
        print(f"\n--- Login as convtest ---")
        ok = await login(page)
        if ok:
            print("  login OK")
        else:
            print("  login FAILED")
            all_errors["login"] = ["登录失败"]

        for name, path, label in AUTH_PAGES:
            errs = await shoot(page, name, path)
            all_errors[name] = errs

        # 3) 商品详情（直接访问固定 SKU001）
        try:
            errs = await shoot(page, "detail", "/shop/SKU001")
            all_errors["detail"] = errs
        except Exception as e:
            print(f"\n[detail] FAILED: {e}")

        # 4) M9.5 上下文贯通测试：点详情页"咨询客服"按钮 → 看 chat 是不是带 sku context
        try:
            print(f"\n[ctx-test] 商品详情 → 点咨询客服 → chat 应自动发问")
            await page.goto(f"{BASE}/shop/SKU001", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)
            ask_btn = await page.query_selector("button.ask-btn-primary")
            if ask_btn:
                await ask_btn.click()
                await page.wait_for_url(lambda url: "/chat" in url, timeout=10000)
                await page.wait_for_timeout(4000)  # 等 SSE 流式回答
                out = OUT / "ctx-product.png"
                await page.screenshot(path=str(out), full_page=True)
                print(f"  SCREEN: {out.name} ({out.stat().st_size // 1024} KB)")
                # 验证：AI 回答里应出现 "ZP1" 或 "12+256"（商品特征词）
                content = await page.content()
                if "ZP1" in content or "12+256" in content or "5999" in content:
                    print(f"  ✓ context passthrough OK: AI 提到了商品特征")
                else:
                    print(f"  ✗ context 丢失: AI 没提到商品")
                    all_errors.setdefault("ctx-product", []).append("商品特征词未出现")
            else:
                print("  ask-btn-primary not found")
        except Exception as e:
            print(f"\n[ctx-product] FAILED: {e}")
            all_errors.setdefault("ctx-product", []).append(str(e))

        # 5) M9.5 上下文贯通测试：订单卡片"咨询客服"按钮 → chat 应自动发问
        try:
            print(f"\n[ctx-test] 订单卡片 → 点咨询客服 → chat 应自动发问")
            await page.goto(f"{BASE}/profile", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
            ask_btn = await page.query_selector(".order-card .ask-btn")
            if ask_btn:
                await ask_btn.click()
                await page.wait_for_url(lambda url: "/chat" in url, timeout=10000)
                await page.wait_for_timeout(4000)
                out = OUT / "ctx-order.png"
                await page.screenshot(path=str(out), full_page=True)
                print(f"  SCREEN: {out.name} ({out.stat().st_size // 1024} KB)")
                content = await page.content()
                if "ORD" in content:
                    print(f"  ✓ context passthrough OK: AI 提到了订单号")
                else:
                    print(f"  ✗ context 丢失: AI 没提到订单")
                    all_errors.setdefault("ctx-order", []).append("订单号未出现")
            else:
                print("  order ask-btn not found (no orders?)")
        except Exception as e:
            print(f"\n[ctx-order] FAILED: {e}")
            all_errors.setdefault("ctx-order", []).append(str(e))

        await browser.close()

    # 汇总
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    total_errors = sum(len(v) for v in all_errors.values())
    for name, errs in all_errors.items():
        status = "OK" if not errs else f"WARN {len(errs)} err"
        print(f"  {status:14s} {name}")
    print(f"\nTotal errors: {total_errors}")
    print(f"Screenshots:  {OUT}")

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))