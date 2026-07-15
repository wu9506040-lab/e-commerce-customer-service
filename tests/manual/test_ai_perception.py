"""
Sprint P2 / SSE Resume: AI 感知测试（半自动）

5 个典型 query 跑一遍，模拟断流 → 自动 resume → 用户视角检查

判定标准（用户拍板 MVP 边界）：
- PASS：消息连续，无"网络中断""续传中""AI 正在补救"等提示，无 error banner
- FAIL：出现技术提示 / error banner / 明显内容截断

工具：
- playwright 1.59.0（已装）
- page.context().set_offline(True) 模拟断网
- 用纯文本提取 final assistant message

Windows GBK 终端兼容：所有 print 用 ASCII（PASS/FAIL/CAPTURE），中文 query 通过 stdin 输入或 hardcode

运行：
    python tests/manual/test_ai_perception.py
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

# =============================================================
# 配置
# =============================================================
FRONTEND_URL = "http://localhost:5173"
LOGIN_URL = f"{FRONTEND_URL}/login"
CHAT_URL = f"{FRONTEND_URL}/chat"
USERNAME = "sse_test"
PASSWORD = "sse_test_123"

# 5 个典型 query（覆盖不同场景：政策/订单/商品/推荐/闲聊）
TEST_QUERIES = [
    ("policy", "refund policy query"),
    ("order", "order status query"),
    ("product", "product price query"),
    ("recommend", "recommend gift query"),
    ("chitchat", "smalltalk query"),
]

# 中文 query（实际发给后端的文本）
QUERIES_ZH = {
    "policy": "退款流程是怎样的？",
    "order": "订单 ORD20260622003 怎么还没到？",
    "product": "iPhone 15 多少钱？",
    "recommend": "送女朋友什么礼物好？",
    "chitchat": "你好",
}

# AI 暴露关键词（不应出现在 UI 中）
AI_DISCLOSURE_WORDS = [
    "network error", "网络中断", "续传", "AI 正在", "智能客服",
    "正在补救", "断网", "重连", "stream", "resume",
]

# 截图目录
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)


# =============================================================
# 测试用例
# =============================================================
async def login(page: Page) -> None:
    """登录流程"""
    await page.goto(LOGIN_URL)
    # 等 form 加载
    await page.wait_for_selector('input[placeholder*="用户名"]', timeout=10000)
    await page.fill('input[placeholder*="用户名"]', USERNAME)
    await page.fill('input[type="password"]', PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_url(f"{CHAT_URL}**", timeout=10000)


async def send_query(page: Page, query: str) -> None:
    """发送 query（按 Enter）"""
    # textarea 是 MessageInput 组件
    textarea = page.locator("textarea").first
    await textarea.fill(query)
    await textarea.press("Enter")


async def get_final_assistant_text(page: Page) -> str:
    """提取最后一个 assistant 消息的纯文本"""
    return await page.evaluate(
        """() => {
            const msgs = document.querySelectorAll('.message.assistant');
            if (!msgs.length) return '';
            const last = msgs[msgs.length - 1];
            const bubble = last.querySelector('.bubble');
            return (bubble || last).innerText.trim();
        }"""
    )


async def has_error_banner(page: Page) -> bool:
    """检查是否有 error banner（暴露技术错误给用户）"""
    return await page.locator(".error-banner").count() > 0


async def run_single_query(
    page: Page, scenario: str, query: str, idx: int
) -> dict:
    """跑一个 query：发起 → 等待流 → 断流 → 恢复 → 等完成 → 检查结果"""
    print(f"  [{idx}] query: {query}")

    # 1. 发起
    await send_query(page, query)
    await asyncio.sleep(1.5)  # 等流开始（缩短，模拟"中途断网"）

    # 2. 截图：断流前
    await page.screenshot(path=str(SCREENSHOT_DIR / f"02_before_{scenario}.png"))

    # 3. 模拟断流（offline 模式 + 等 LLM 继续吐 token）
    await page.context.set_offline(True)
    await asyncio.sleep(2.0)  # 缩短，让 resume 更快入位

    # 4. 恢复网络
    await page.context.set_offline(False)

    # 5. 等流结束 / 等最终消息收敛（轮询 DOM 状态）
    #    终止条件：流结束（无 streaming indicator / 无 .bubble.streaming）
    #    OR 出现 error banner（用户视角最坏情况）
    settled = False
    for _ in range(30):  # 30 * 0.5 = 15s max
        await asyncio.sleep(0.5)
        state = await page.evaluate(
            """() => {
                const ind = document.querySelector('.streaming-indicator');
                const btn = document.querySelector('button');
                const err = document.querySelector('.error-banner');
                return {
                    streaming: !!ind,
                    sending: btn && btn.textContent && btn.textContent.indexOf('生成') >= 0,
                    error: !!err,
                };
            }"""
        )
        if not state["streaming"] and not state["sending"]:
            settled = True
            break
        if state["error"]:
            settled = True  # 也算"结束"
            break
    if not settled:
        print(f"      WARN: did not settle within 15s, snapshot current state")

    # 6. 截图：最终状态
    await page.screenshot(
        path=str(SCREENSHOT_DIR / f"03_after_{scenario}.png"), full_page=True
    )

    # 7. 提取结果
    final_text = await get_final_assistant_text(page)
    has_error = await has_error_banner(page)
    error_text = ""
    if has_error:
        error_text = await page.locator(".error-banner").inner_text()

    # 8. 检查 AI 暴露关键词
    has_disclosure = any(w in final_text for w in AI_DISCLOSURE_WORDS)
    has_disclosure_in_error = (
        any(w in error_text for w in AI_DISCLOSURE_WORDS) if error_text else False
    )

    # 9. 检查内容长度（resume 限流：< 5 字算"明显截断"）
    looks_truncated = len(final_text) < 5

    # 10. 判定
    passed = not has_disclosure and not has_disclosure_in_error and not has_error and not looks_truncated
    status = "PASS" if passed else "FAIL"
    print(f"      status: {status}")
    print(f"      final_text_len: {len(final_text)}")
    print(f"      has_error_banner: {has_error}")
    print(f"      looks_truncated: {looks_truncated}")
    print(f"      final_text_preview: {final_text[:80]!r}")

    return {
        "scenario": scenario,
        "query": query,
        "status": status,
        "final_text": final_text,
        "final_text_len": len(final_text),
        "has_error_banner": has_error,
        "error_text": error_text,
        "has_disclosure": has_disclosure or has_disclosure_in_error,
        "looks_truncated": looks_truncated,
    }


async def main() -> int:
    """主流程：登录 → 跑 5 个 query → 汇总"""
    print("=" * 60)
    print("Sprint P2 / SSE Resume: AI Perception Test")
    print("=" * 60)

    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = await context.new_page()

        # 诊断日志：捕获 console + 网络响应
        console_logs: list[str] = []
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: console_logs.append(f"[pageerror] {err}"))
        responses: list[str] = []
        page.on(
            "response",
            lambda r: responses.append(f"{r.status} {r.request.method} {r.url}"),
        )
        failed_requests: list[str] = []
        page.on(
            "requestfailed",
            lambda r: failed_requests.append(f"{r.failure} {r.method} {r.url}"),
        )

        # 1. 登录（一次性，5 个 query 共用同一会话）
        print("\n[login] ...")
        try:
            await login(page)
        except Exception as e:
            print(f"[login] FAIL: {e}")
            await page.screenshot(path=str(SCREENSHOT_DIR / "00_login_failed.png"))
            for log in console_logs[-20:]:
                print(f"  console: {log}")
            await browser.close()
            return 1

        # 2. 截图：登录后
        await page.screenshot(path=str(SCREENSHOT_DIR / "01_after_login.png"))

        # 3. 跑 5 个 query
        for idx, (scenario, _) in enumerate(TEST_QUERIES, 1):
            query = QUERIES_ZH[scenario]
            try:
                result = await run_single_query(page, scenario, query, idx)
                results.append(result)
            except Exception as e:
                print(f"      EXCEPTION: {e}")
                results.append(
                    {"scenario": scenario, "query": query, "status": "FAIL", "error": str(e)}
                )

        await browser.close()

    # =============================================================
    # 汇总
    # =============================================================
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    total = len(results)
    passed = sum(1 for r in results if r.get("status") == "PASS")
    print(f"PASS: {passed}/{total}")

    for r in results:
        status = r.get("status", "FAIL")
        scenario = r.get("scenario", "?")
        text_len = r.get("final_text_len", 0)
        err = " (error)" if r.get("has_error_banner") else ""
        trunc = " (truncated)" if r.get("looks_truncated") else ""
        disc = " (disclosure)" if r.get("has_disclosure") else ""
        print(f"  [{status}] {scenario}: len={text_len}{err}{trunc}{disc}")

    # 阈值：至少 4/5 PASS 才算达成"用户察觉不到 AI"
    threshold = 4
    final = "PASS" if passed >= threshold else "FAIL"
    print(f"\nFinal: {final} (need >= {threshold}/{total} to PASS)")

    # 诊断日志：失败时输出 console + 网络
    if final == "FAIL":
        print("\n=== Diagnostic Console Logs (last 30) ===")
        for log in console_logs[-30:]:
            print(f"  {log}")
        print("\n=== Diagnostic Network (chat-related, all) ===")
        for r in responses:
            if "/chat" in r or "/auth" in r or "/me" in r:
                print(f"  {r}")
        print("\n=== Failed Requests (offline / 4xx) ===")
        for r in failed_requests[-30:]:
            print(f"  {r}")

    return 0 if final == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
