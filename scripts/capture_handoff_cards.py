"""scripts/capture_handoff_cards.py — 公网 HandoffCard 优先级三色存证

对 http://120.79.27.124:5173 真实公网 V3 环境：
- 用 API 注册+登录拿 httpOnly Cookie（注入 playwright context）
- 导航 /chat?q=<话术> 自动发送
- 等待 .handoff-card 渲染，按 data-priority 截图

用法：python scripts/capture_handoff_cards.py <priority> <query>
  priority: P0 / P1 / P2（仅用于命名与断言）
"""
import sys
import time
from playwright.sync_api import sync_playwright

BASE = "http://120.79.27.124:5173"
API = "http://120.79.27.124:5173/api"
OUT = "docs/reports/m14_v3_handoff_cards"


def main(priority: str, query: str) -> int:
    user = f"card_{priority.lower()}_{int(time.time())}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})

        # 1. register + login via API（Cookie 落到 context）
        r = ctx.request.post(
            f"{API}/auth/register",
            data={"username": user, "password": "Test123456", "email": f"{user}@t.com"},
            headers={"Content-Type": "application/json"},
        )
        print(f"register: {r.status}", flush=True)
        r = ctx.request.post(
            f"{API}/auth/login",
            form={"username": user, "password": "Test123456"},
        )
        print(f"login: {r.status}", flush=True)

        # 2. 打开 chat 并自动发送
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))
        page.goto(f"{BASE}/chat?q={query}", wait_until="domcontentloaded", timeout=30000)

        # 3. 等 handoff-card 出现（公网 LLM + V3 三层延迟，给足 60s）
        card = None
        try:
            page.wait_for_selector(".handoff-card", timeout=60000)
            card = page.locator(".handoff-card").first
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"NO handoff-card: {e}", flush=True)

        if card:
            dp = card.get_attribute("data-priority")
            print(f"data-priority={dp}", flush=True)
            card.screenshot(path=f"{OUT}/handoff_{priority}_element.png")
            page.screenshot(path=f"{OUT}/handoff_{priority}_fullpage.png", full_page=True)
            print(f"SCREENSHOT saved handoff_{priority}_element.png", flush=True)
            match = "PASS" if dp == priority else f"MISMATCH(got {dp})"
            print(f"assert priority=={priority}: {match}", flush=True)
        print(f"console_errors={len(errors)}", flush=True)
        for e in errors[:5]:
            print(f"  ERR: {e[:120]}", flush=True)
        browser.close()
    return 0


if __name__ == "__main__":
    pri = sys.argv[1] if len(sys.argv) > 1 else "P0"
    q = sys.argv[2] if len(sys.argv) > 2 else "我要投诉12315，你们太坑了"
    sys.exit(main(pri, q))
