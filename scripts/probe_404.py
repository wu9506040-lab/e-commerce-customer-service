"""快速 probe 看 ECS/本地 哪些资源 404"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await context.new_page()

        failed_urls = []
        def on_response(resp):
            if resp.status >= 400:
                failed_urls.append(f"{resp.status} {resp.url}")
        page.on("response", on_response)

        await page.goto("http://localhost:5173/", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)

        print(f"Total 4xx/5xx responses: {len(failed_urls)}")
        for u in failed_urls[:20]:
            print(f"  {u}")
        await browser.close()

asyncio.run(main())