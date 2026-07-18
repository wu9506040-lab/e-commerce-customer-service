"""
Case D 视觉验证：转人工兜底 HandoffCard
========================================

目的：用 Playwright 登录 demotest → 多轮对话填 recent_messages → 发"我要转人工"
      → 截图 HandoffCard 给用户肉眼验证视觉

输出：frontend/_screenshots/manual/case-d-handoff.png
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://localhost:5173"
OUT_DIR = Path(__file__).parent.parent / "frontend" / "_screenshots" / "manual"
OUT_DIR.mkdir(parents=True, exist_ok=True)


async def main():
    print("=" * 70)
    print("  Case D 视觉验证：转人工兜底 HandoffCard")
    print("=" * 70)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = await context.new_page()

        console_errors = []
        page.on("pageerror", lambda exc: console_errors.append(f"PAGEERROR: {exc}"))
        page.on("console", lambda msg: console_errors.append(f"CONSOLE_ERR: {msg.text[:200]}")
                if msg.type == "error" else None)

        # 1. 登录 demotest
        print("\n[1/5] 登录 demotest")
        await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("input[autocomplete='username']", timeout=10000)
        await page.fill("input[autocomplete='username']", "demotest")
        await page.fill("input[autocomplete='current-password']", "demotest123")
        await page.click("button[type='submit']")
        await page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        print(f"  OK  跳转到: {page.url}")

        # 2. 进入聊天页
        print("\n[2/5] 跳转 /chat")
        await page.goto(f"{BASE}/chat", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("input[type='text'], textarea", timeout=10000)

        # 3. 发 3 轮对话填历史（让 recent_messages 有内容）
        print("\n[3/5] 填充多轮对话（让 recent_messages 有内容）")
        for q in ["退货政策是什么？", "7 天无理由包括什么？", "运费谁出？"]:
            await page.fill("input[type='text'], textarea", q)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)  # 等 SSE 流式回答
            print(f"  OK  问: {q}")

        # 4. 发"我要转人工"
        print("\n[4/5] 发送: 我要转人工")
        await page.fill("input[type='text'], textarea", "我要转人工")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(4000)  # 等 handoff meta + token 流式

        # 5. 截图 HandoffCard
        print("\n[5/5] 截图 HandoffCard")
        await page.wait_for_selector(".handoff-card", timeout=8000)
        # 滚到底部看到 HandoffCard
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        out_path = OUT_DIR / "case-d-handoff.png"
        await page.screenshot(path=str(out_path), full_page=True)
        print(f"  OK  截图: {out_path} ({out_path.stat().st_size // 1024} KB)")

        # 提取 HandoffCard 关键信息做断言
        handoff_text = await page.text_content(".handoff-card")
        import re
        handoff_id_match = re.search(r"H[A-F0-9]{8}", handoff_text or "")
        reason_match = re.search(r"(用户主动|系统异常|业务规则)", handoff_text or "")
        has_user_card = "demotest" in (handoff_text or "")
        has_orders = "ORD" in (handoff_text or "")
        has_summary = "摘要" in (handoff_text or "") or "上下文" in (handoff_text or "")

        print("\n" + "=" * 70)
        print("  HandoffCard 内容断言")
        print("=" * 70)
        print(f"  handoff_id 格式正确:  {bool(handoff_id_match)}  ({handoff_id_match.group(0) if handoff_id_match else 'NONE'})")
        print(f"  reason 标签命中:        {bool(reason_match)}  ({reason_match.group(0) if reason_match else 'NONE'})")
        print(f"  用户名片 (demotest):    {has_user_card}")
        print(f"  订单号 (ORD...):        {has_orders}")
        print(f"  摘要/上下文:            {has_summary}")

        # 控制台错误
        unique_errors = [e for e in console_errors if "favicon" not in e.lower()]
        print(f"\n  控制台错误数: {len(unique_errors)}")
        for e in unique_errors[:3]:
            print(f"    - {e}")

        await browser.close()

        # 断言
        ok = (handoff_id_match is not None
              and reason_match is not None
              and has_user_card
              and has_orders
              and len(unique_errors) == 0)
        print("\n" + "=" * 70)
        print(f"  Case D 验证: {'PASS' if ok else 'FAIL'}")
        print("=" * 70)
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))