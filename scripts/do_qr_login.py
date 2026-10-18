"""do_qr_login.py - 容器内用 Playwright 拉闲鱼登录 QR + 落 cookie"""
import asyncio
import base64
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

LOGIN_URL = "https://www.goofish.com/login"
TIMEOUT = 180


async def wait_for_qr(page, timeout: int = 30) -> str:
    """等 .qrcode-login img, passport 嵌 iframe 递归遍历"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        all_frames = [page]

        def collect(p):
            try:
                for f in p.frames:
                    all_frames.append(f)
                    collect(f)
            except Exception:
                pass
        collect(page)
        for f in all_frames:
            try:
                for sel in [".qrcode-login img", ".qrcode img", "img[src*='data:image']"]:
                    qr = await f.query_selector(sel)
                    if qr:
                        src = await qr.get_attribute("src")
                        if src and src.startswith("data:image"):
                            return src
            except Exception:
                continue
        await asyncio.sleep(0.5)
    print("===DEBUG url=", page.url, flush=True)
    print("===title=", await page.title(), flush=True)
    for i, f in enumerate(page.frames):
        try:
            print("frame", i, "url=", f.url, flush=True)
        except Exception:
            pass
    content = await page.content()
    print("===HTML_DUMP_START===")
    sys.stdout.buffer.write(content[:3000].encode("utf-8", errors="replace"))
    print("===HTML_DUMP_END===")
    import re
    qr_refs = re.findall(r"(qrcode[^\s\"'<>]*)", content, re.IGNORECASE)
    print("===qrcode refs:", qr_refs[:20], flush=True)
    raise RuntimeError("QR 未在 {}s 内出现".format(timeout))


def has_login_cookies(cookies):
    need = {"_m_h5_tk", "unb", "cookie2"}
    return need.issubset({c["name"] for c in cookies})


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

        print("Navigating to", LOGIN_URL, flush=True)
        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        print("after networkidle + 3s, url=", page.url, flush=True)

        print("Waiting for QR code...", flush=True)
        qr_src = await wait_for_qr(page, timeout=30)

        print("===QR_CODE_BASE64_START===", flush=True)
        print(qr_src, flush=True)
        print("===QR_CODE_BASE64_END===", flush=True)

        b64 = qr_src.split(",", 1)[1]
        qr_path = Path.home() / ".goofish-cli" / "qr.png"
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_path.write_bytes(base64.b64decode(b64))
        print("QR saved to", qr_path, flush=True)

        print("请用手机闲鱼 App 扫码（{}s 超时）".format(TIMEOUT), flush=True)
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            cookies = await context.cookies()
            if has_login_cookies(cookies):
                print("扫码成功! 拿到", len(cookies), "cookies", flush=True)
                break
            await asyncio.sleep(1.0)
        else:
            print("TIMEOUT - 扫码超时", flush=True)
            await browser.close()
            return 1

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
        from goofish_cli.core.session import (
            DEFAULT_COOKIE_PATH,
            write_cookies_json,
        )
        write_cookies_json(DEFAULT_COOKIE_PATH, records)
        print("cookie 已写到", DEFAULT_COOKIE_PATH, flush=True)

        await browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
