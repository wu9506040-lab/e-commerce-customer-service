"""scripts/verify_handoff_cards.py

M14 V3 HandoffCard 三色优先级 playwright 截图验证脚本

流程（按公网 DemoLanding → demoLogin → ChatPage）：
1. 打开 http://120.79.27.124:5173/demo
2. 点击「立即体验 demo」按钮 → demoLogin() → 自动跳 /chat
3. 等 ChatPage 的 textarea 出现
4. 依次输入 3 类 query（P0 高风险 / P1 一般 / P2 用户主动升级）
5. 等 SSE meta.handoff → 触发 .handoff-card 渲染
6. 截元素图 + 全页图 → docs/reports/m14_v3_handoff_cards/
7. 校验 data-priority + --handoff-color CSS 变量

按 feedback_windows_gbk_playwright.md 用 ASCII 输出 (PASS/FAIL/SCREEN)
"""
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# =============================================================
# 配置
# =============================================================
PUBLIC_URL = "http://120.79.27.124:5173"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "reports" / "m14_v3_handoff_cards"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 3 类触发 query（按 chat.py P0 前置拦截 + RefundFlow V3 决策）
# 注意：用户是 demotest 账号，已经有历史订单
TEST_CASES = [
    {
        "id": "P0",
        "query": "我要投诉 12315 消费者协会",
        "expected_priority": "P0",
        "expected_color": "#cf1322",  # 朱红
        "description": "P0 high risk keyword (complaint category)",
    },
    {
        "id": "P1",
        "query": "商品破损了但是我没拍照凭证",
        "expected_priority": "P1",
        "expected_color": "#fa8c16",  # 橙黄
        "description": "P1 quality issue without proof",
    },
    {
        "id": "P2",
        "query": "我要转人工",
        "expected_priority": "P2",
        "expected_color": "#8c8c8c",  # 灰
        "description": "P2 user-requested escalation",
    },
]

LOGIN_TIMEOUT = 30_000
CHAT_RESPONSE_TIMEOUT = 60_000  # 公网 LangGraph 12s+12s + buffer
WAIT_AFTER_SEND = 2  # 发送后给前端时间处理


def log(msg: str) -> None:
    """ASCII-only log to avoid Windows GBK UnicodeEncodeError."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def verify_color(actual: str, expected: str) -> bool:
    """颜色比对（允许大小写差异 / 空格差异）"""
    if not actual:
        return False
    return actual.lower().replace(" ", "") == expected.lower()


def auto_login_via_demo(page) -> None:
    """点 DemoLanding 「立即体验 demo」按钮 → demoLogin → 跳 /chat

    这是公网最稳的登录路径（绕开 LoginPage 表单 + 避免 demotest/demotest123 拼写错）
    """
    log("=== Auto login via DemoLanding 'demo' button ===")
    page.goto(PUBLIC_URL, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT)
    log(f"  navigated to {PUBLIC_URL} -> {page.url}")

    # DemoLanding 是 /demo 路径
    if "/demo" not in page.url:
        log(f"  WARNING: not on /demo, current URL: {page.url}")

    # 等 DemoLanding 渲染
    page.wait_for_selector("text=立即体验", timeout=LOGIN_TIMEOUT)
    log(f"  DemoLanding rendered")

    # 点「立即体验 demo」按钮（DemoLanding.vue:243）
    # 按钮文字包含 "立即体验 demo"
    demo_btn = page.locator("button", has_text="立即体验").first
    demo_btn.click()
    log(f"  'demo' button clicked, waiting for /chat...")

    # 等 ChatPage textarea 出现（MessageInput.vue placeholder="输入消息…"）
    page.wait_for_selector("textarea[placeholder*='输入消息']", timeout=LOGIN_TIMEOUT)
    log(f"  ChatPage loaded, textarea ready")


def run_case(page, tc: dict) -> dict:
    """跑单个 case：输入 query → 等 HandoffCard → 截图 → 验证"""
    log(f"=== Case {tc['id']}: {tc['description']} ===")
    log(f"  query: {tc['query']}")
    log(f"  expected: priority={tc['expected_priority']}, color={tc['expected_color']}")

    # 清空旧消息 + 输入
    input_box = page.locator("textarea[placeholder*='输入消息']").first
    input_box.fill("")
    input_box.fill(tc["query"])
    log(f"  input filled")

    # 找发送按钮（MessageInput.vue 是 button[type='button'] 或默认 type=submit）
    send_btn = page.locator(".input-bar button, textarea + button").first
    if send_btn.count() == 0:
        # fallback：找 textarea 紧邻的 button
        send_btn = page.locator("textarea[placeholder*='输入消息'] ~ button").first
    send_btn.click()
    log(f"  send clicked, waiting for HandoffCard...")

    # 等 HandoffCard 出现（HandoffCard.vue root class="handoff-card"）
    try:
        page.wait_for_selector(".handoff-card", timeout=CHAT_RESPONSE_TIMEOUT)
        log(f"  HandoffCard appeared")
    except PWTimeout:
        log(f"  FAIL: HandoffCard not found within {CHAT_RESPONSE_TIMEOUT}ms")
        page.screenshot(path=str(OUTPUT_DIR / f"handoff_{tc['id']}_TIMEOUT.png"), full_page=True)
        return {"id": tc["id"], "status": "FAIL", "reason": "timeout"}

    # 等 1.5s 让 CSS 变量生效 + 动画完成
    page.wait_for_timeout(1500)

    # 提取 priority 属性 + 计算颜色
    handoff_card = page.locator(".handoff-card").last  # 取最新一条
    actual_priority = handoff_card.get_attribute("data-priority")
    actual_color = handoff_card.evaluate(
        "el => getComputedStyle(el).getPropertyValue('--handoff-color').trim()"
    )

    log(f"  actual: priority={actual_priority}, color={actual_color}")

    priority_ok = actual_priority == tc["expected_priority"]
    color_ok = verify_color(actual_color, tc["expected_color"])
    overall_ok = priority_ok and color_ok

    # 截图（元素级 + 全页）
    element_path = OUTPUT_DIR / f"handoff_{tc['id']}_element.png"
    fullpage_path = OUTPUT_DIR / f"handoff_{tc['id']}_fullpage.png"
    handoff_card.screenshot(path=str(element_path))
    page.screenshot(path=str(fullpage_path), full_page=True)
    log(f"  SCREEN: {element_path.name} + {fullpage_path.name}")

    status = "PASS" if overall_ok else "FAIL"
    log(f"  RESULT: {status} (priority_ok={priority_ok}, color_ok={color_ok})")
    return {
        "id": tc["id"],
        "status": status,
        "expected_priority": tc["expected_priority"],
        "actual_priority": actual_priority,
        "expected_color": tc["expected_color"],
        "actual_color": actual_color,
        "screenshot_element": str(element_path.name),
        "screenshot_fullpage": str(fullpage_path.name),
    }


def main() -> int:
    log("M14 V3 HandoffCard 三色优先级截图验证开始")
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        try:
            auto_login_via_demo(page)
            log("登录成功（demo 自动登录），开始跑 3 个 case")

            for tc in TEST_CASES:
                result = run_case(page, tc)
                results.append(result)

            # 统计
            total = len(results)
            passed = sum(1 for r in results if r["status"] == "PASS")
            failed = total - passed
            log(f"=== Summary ===")
            log(f"  Total: {total}, PASS: {passed}, FAIL: {failed}")
            log(f"  Screenshots: {OUTPUT_DIR}")

            return 0 if failed == 0 else 1
        except Exception as e:
            log(f"ERROR: {type(e).__name__}: {e}")
            page.screenshot(path=str(OUTPUT_DIR / "_FATAL.png"), full_page=True)
            return 2
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())