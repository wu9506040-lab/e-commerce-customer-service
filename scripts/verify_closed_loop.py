"""
M10 闭环电商流程端到端验证

按 CLAUDE.md 工作流：Explore → Plan → Implement → Test
本脚本覆盖完整闭环：登录 → 下单 → 付款 → 发货 → 签收 → 退款

前提：
1. frontend (Vite dev :5173) + backend (FastAPI :8000) + docker-compose 服务都已运行
2. demotest 用户已 seed（python scripts/seed_demo_data.py --reset）
3. playwright 已装（pip install playwright + playwright install chromium）

用法：
    python scripts/verify_closed_loop.py
"""
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright, Page

BASE = "http://localhost:5173"
OUT = Path(__file__).resolve().parent.parent / "frontend" / "_screenshots"
OUT.mkdir(parents=True, exist_ok=True)


def _print_step(step: str, ok: bool, msg: str = ""):
    """统一打印格式：PASS / FAIL（Windows GBK 兼容，不用 unicode 符号）"""
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {step}"
    if msg:
        line += f"  ({msg})"
    print(line)
    return ok


async def login(page: Page) -> bool:
    """登录 demotest"""
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_selector("input[autocomplete='username']", timeout=10000)
    await page.fill("input[autocomplete='username']", "demotest")
    await page.fill("input[autocomplete='current-password']", "demotest123")
    await page.click("button[type='submit']")
    try:
        await page.wait_for_url(lambda url: "/login" not in url, timeout=10000)
        return True
    except Exception:
        return False


async def shot(page: Page, name: str) -> None:
    """截图到 _screenshots/loop-{name}.png"""
    out = OUT / f"loop-{name}.png"
    await page.screenshot(path=str(out), full_page=True)
    print(f"  SCREEN: {out.name}")


async def get_first_status(page: Page) -> str:
    """从个人中心列表读取第一张订单卡的状态徽章文字"""
    # 等待订单列表加载
    await page.wait_for_selector(".status-badge", timeout=8000)
    return (await page.text_content(".status-badge")) or ""


async def click_action_btn(page: Page, label: str) -> bool:
    """点状态流转按钮（label: 立即付款/模拟发货/确认签收/申请退款）"""
    # 找带指定文字的 action-btn（订单卡内）
    btn = page.locator(f".action-btn:has-text('{label}')").first
    if await btn.count() == 0:
        print(f"  WARN: 找不到按钮 {label}")
        return False
    await btn.click()
    # 等 loading 状态结束（按钮恢复 label 或消失）
    await page.wait_for_timeout(1500)
    return True


async def main() -> int:
    print("=" * 60)
    print("M10 闭环电商流程验证")
    print("=" * 60)
    results: list[bool] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # 收集 console 错误
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text)
                if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        # ------------------------------------------------------------
        # Step 1: 登录
        # ------------------------------------------------------------
        print("\n[1] 登录 demotest")
        ok = await login(page)
        results.append(_print_step("login", ok))
        if not ok:
            await shot(page, "login-failed")
            await browser.close()
            return 1
        await page.wait_for_timeout(1000)

        # ------------------------------------------------------------
        # Step 2: 进入商品详情
        # ------------------------------------------------------------
        print("\n[2] 打开商品详情 /shop/SKU001")
        await page.goto(f"{BASE}/shop/SKU001", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector(".buy-btn-primary", timeout=10000)
        await shot(page, "01-product-detail")
        results.append(_print_step("打开商品详情页", True))

        # ------------------------------------------------------------
        # Step 3: 点立即购买 → 跳 /profile，列表里出现新 pending 订单
        # ------------------------------------------------------------
        print("\n[3] 点立即购买")
        await page.click(".buy-btn-primary")
        await page.wait_for_url(lambda url: "/profile" in url, timeout=10000)
        await page.wait_for_selector(".order-card", timeout=10000)
        await page.wait_for_timeout(1500)
        await shot(page, "02-profile-pending")

        # 验证：第一张订单（应该是新下的）状态是 待支付
        status = (await get_first_status(page)).strip()
        results.append(_print_step("下单后跳到 /profile",
                                   True,
                                   f"首单状态={status}"))
        results.append(_print_step("首单状态=待支付", status == "待支付"))

        # 记下第一张订单的 order_no 用于后续校验
        first_order_no = await page.text_content(".order-card .order-no")
        first_order_no = (first_order_no or "").replace("订单号 ", "").strip()
        print(f"  INFO: 首单 order_no = {first_order_no}")

        # ------------------------------------------------------------
        # Step 4: 点 [立即付款] → paid
        # ------------------------------------------------------------
        print("\n[4] 点 立即付款 (pending → paid)")
        ok = await click_action_btn(page, "立即付款")
        results.append(_print_step("点击立即付款", ok))
        await page.wait_for_timeout(800)
        await shot(page, "03-profile-paid")
        status = (await get_first_status(page)).strip()
        results.append(_print_step("付款后状态=已支付",
                                   status == "已支付",
                                   f"actual={status}"))

        # ------------------------------------------------------------
        # Step 5: 点 [模拟发货] → shipped
        # ------------------------------------------------------------
        print("\n[5] 点 模拟发货 (paid → shipped)")
        ok = await click_action_btn(page, "模拟发货")
        results.append(_print_step("点击模拟发货", ok))
        await page.wait_for_timeout(800)
        await shot(page, "04-profile-shipped")
        status = (await get_first_status(page)).strip()
        results.append(_print_step("发货后状态=运输中",
                                   status == "运输中",
                                   f"actual={status}"))

        # ------------------------------------------------------------
        # Step 6: 点 [确认签收] → delivered
        # ------------------------------------------------------------
        print("\n[6] 点 确认签收 (shipped → delivered)")
        ok = await click_action_btn(page, "确认签收")
        results.append(_print_step("点击确认签收", ok))
        await page.wait_for_timeout(800)
        await shot(page, "05-profile-delivered")
        status = (await get_first_status(page)).strip()
        results.append(_print_step("签收后状态=已签收",
                                   status == "已签收",
                                   f"actual={status}"))

        # ------------------------------------------------------------
        # Step 7: 点 [申请退款] → refunded
        # ------------------------------------------------------------
        print("\n[7] 点 申请退款 (delivered → refunded)")
        # 自动接受 prompt("请输入退款原因")
        page.once("dialog", lambda d: asyncio.create_task(d.accept("测试脚本自动退款")))
        ok = await click_action_btn(page, "申请退款")
        results.append(_print_step("点击申请退款", ok))
        await page.wait_for_timeout(1500)
        await shot(page, "06-profile-refunded")
        status = (await get_first_status(page)).strip()
        results.append(_print_step("退款后状态=已退款",
                                   status == "已退款",
                                   f"actual={status}"))

        await browser.close()

    # ------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------
    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  {passed}/{total} 检查通过")
    if console_errors:
        print(f"\n  WARN: Console 错误 {len(console_errors)} 条:")
        for e in console_errors[:5]:
            print(f"    - {str(e)[:160]}")
    print(f"\n  截图目录: {OUT}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))