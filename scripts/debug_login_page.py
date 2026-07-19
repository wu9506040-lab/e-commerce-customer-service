"""scripts/debug_login_page.py — diagnose what's on the public page"""
import time
from playwright.sync_api import sync_playwright

PUBLIC_URL = "http://120.79.27.124:5173"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.goto(PUBLIC_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(5000)  # 等 Vue mount + 静态资源加载

    # screenshot
    page.screenshot(path="docs/reports/m14_v3_handoff_cards/_debug_login.png", full_page=True)

    # dump key info
    print("=== URL ===", flush=True)
    print(page.url, flush=True)

    print("=== Title ===", flush=True)
    print(page.title(), flush=True)

    print("=== All input placeholders ===", flush=True)
    inputs = page.locator("input").all()
    for i, inp in enumerate(inputs):
        ph = inp.get_attribute("placeholder") or ""
        typ = inp.get_attribute("type") or ""
        print(f"  [{i}] type={typ} placeholder={ph!r}", flush=True)

    textareas = page.locator("textarea").all()
    print(f"=== {len(textareas)} textareas ===", flush=True)
    for i, ta in enumerate(textareas):
        ph = ta.get_attribute("placeholder") or ""
        print(f"  [{i}] placeholder={ph!r}", flush=True)

    print("=== All buttons ===", flush=True)
    btns = page.locator("button").all()
    for i, b in enumerate(btns):
        try:
            txt = b.inner_text()[:30]
        except Exception:
            txt = "<no text>"
        print(f"  [{i}] text={txt!r}", flush=True)

    browser.close()
