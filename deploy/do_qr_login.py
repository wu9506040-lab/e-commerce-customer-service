"""do_qr_login.py - 容器内用 Playwright 拉闲鱼登录 QR + 落 cookie

用法: docker exec -it xianyu-daemon python do_qr_login.py

流程:
  1. Playwright headless Chromium 打开 passport.goofish.com 登录页
  2. 轮询 .qrcode-login 容器, 提取 QR img src (base64 data URL)
  3. 输出到 stdout (用户 docker logs 看)
  4. 持续轮询 context.cookies() 等扫码 + 手机端确认
  5. 全部 _m_h5_tk / unb / cookie2 到位后写到 ~/.goofish-cli/cookies.json (Fernet 加密)
"""
import asyncio
import base64
import os
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

# 跟 goofish_cli 路径一致
LOGIN_URL = "https://passport.goofish.com/mini_login.htm?appName=goofish&fromSite=77&styleType=vertical"
TIMEOUT = 180  # 3 min 等扫码


async def wait_for_qr(page, timeout: int = 30) -> str:
    """等 .qrcode-login 容器出现, 提取 base64 QR data URL"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # 找所有 frame 里的 .qrcode-login
            frames = page.frames
            for f in frames:
                try:
                    qr = await f.query_selector(".qrcode-login img")
                except Exception:
                    continue
                if qr:
                    src = await qr.get_attribute("src")
                    if src and src.startswith("data:image"):
                        return src
            await asyncio.sleep(0.5)
        except Exception:
            await asyncio.sleep(1.0)
    raise RuntimeError(f"QR 未在 {timeout}s 内出现")


def has_login_cookies(cookies: list) -> bool:
    need = {"_m_h5_tk", "unb", "cookie2"}
    names = {c["name"] for c in cookies}
    return need.issubset(names)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        print(f"Navigating to {LOGIN_URL}", file=sys.stderr)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # 1. 拉 QR
        print("Waiting for QR code...", file=sys.stderr)
        qr_src = await wait_for_qr(page, timeout=30)
        print(f"===QR_CODE_BASE64_START===", file=sys.stderr)
        print(qr_src, flush=True)
        print(f"===QR_CODE_BASE64_END===", file=sys.stderr)

        # 2. 写 base64 PNG 到文件, 方便 docker cp
        # qr_src 形如 "data:image/png;base64,XXXXX"
        b64 = qr_src.split(",", 1)[1]
        qr_path = Path.home() / ".goofish-cli" / "qr.png"
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_path.write_bytes(base64.b64decode(b64))
        print(f"QR saved to {qr_path}", file=sys.stderr)

        # 3. 等用户扫码 + 手机端确认
        print(f"请用手机闲鱼 App 扫码（{TIMEOUT}s 超时）", file=sys.stderr)
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            cookies = await context.cookies()
            if has_login_cookies(cookies):
                print(f"扫码成功! 拿到 {len(cookies)} cookies", file=sys.stderr)
                break
            await asyncio.sleep(1.0)
        else:
            print("TIMEOUT - 扫码超时", file=sys.stderr)
            await browser.close()
            return 1

        # 4. 写 goofish_cli 格式的 cookies.json
        records = [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            for c in cookies
            if c.get("name") and c.get("value")
        ]

        # 用 goofish_cli 的 write_cookies_json（Fernet 加密，机器绑定）
        from goofish_cli.core.session import (
            DEFAULT_COOKIE_PATH,
            write_cookies_json,
        )
        write_cookies_json(DEFAULT_COOKIE_PATH, records)
        print(f"cookie 已写到 {DEFAULT_COOKIE_PATH}", file=sys.stderr)

        await browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
