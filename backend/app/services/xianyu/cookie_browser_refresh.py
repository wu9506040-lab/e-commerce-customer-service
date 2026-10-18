"""
CookieBrowserRefresh — L3 Playwright 免密登录刷新（V18 抄 goofish_cli core/refresh.py + browser.py）

参考实现要点（refresh.py 模块 docstring，全是踩坑实录）：
  1. _m_h5_tk 只有 10 分钟有效期，服务端只在浏览器"活跃访问"时 Set-Cookie 续期
  2. 浏览器指纹不对时服务端把注入 cookie 视为"半失效 session"，首页会弹 passport
     登录框 —— 弹窗里有「快速进入」（免密记忆登录）按钮，自动点一下服务端就重新
     下发完整 session cookie，用户无感
  3. 流程：goto 首页 → 点「快速进入」→ goto /bought（强鉴权页，触发下发
     cookie2/sgcookie/_tb_token_）→ 抓 context.cookies()
  4. 刷新成功必须见到 cookie2 被下发（光有 _m_h5_tk/unb 仍会 SESSION_EXPIRED）
  5. 用系统 Chrome（channel="chrome"），不用 bundled chromium（指纹裸奔是风控高危）
  6. 默认 headful —— headless Chrome 会被闲鱼判「非法访问」（GOOFISH_HEADLESS=1 可切）
  7. 每次独立 tmp profile —— Chrome user_data_dir 有 SingletonLock，固定路径并发会挂

我们的差异：
  - cookie 源/落盘是 cookie.txt（flat k=v 串），不是 goofish-cli 的加密 cookies.json
  - 触发：daemon 续期循环里 mtop 连续失败 ≥3 次（EXOIRED/SESSION_EXPIRED 立即，
    ILLEGAL 按参考结论属风控层救不了，但也做 best-effort 尝试），冷却默认 30min
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

HOME_URL = "https://www.goofish.com"
# 强鉴权页：goto 后服务端必须下发完整 session cookie（cookie2/sgcookie/_tb_token_）
AUTH_PROBE_URL = "https://www.goofish.com/bought"
# 刷新成功的必需字段（抄 _REQUIRED_FRESH_COOKIES）
REQUIRED_FRESH_COOKIES = ("_m_h5_tk", "unb", "cookie2")

# 淘系签名链路 cookie（历史上跨 .taobao.com），其余默认落 .goofish.com
# （抄 browser.py _legacy_domain：仅用于无 domain 信息的 flat cookie）
_TAOBAO_COOKIE_NAMES = {"_m_h5_tk", "_m_h5_tk_enc", "x5sec", "sgcookie", "cookie2", "_tb_token_"}


def _legacy_domain(name: str) -> str:
    """按 cookie 名字反推归属域（抄 browser.py，用于 flat cookie 无 domain 场景）"""
    return ".taobao.com" if name in _TAOBAO_COOKIE_NAMES else ".goofish.com"


def _flat_to_playwright(flat: dict[str, str]) -> list[dict]:
    """cookie.txt 的 flat dict → playwright add_cookies 列表（抄 _cookies_to_playwright）"""
    import time as _time
    expires = int(_time.time()) + 7 * 24 * 3600
    out = []
    for name, value in flat.items():
        if not value:
            continue
        out.append({
            "name": name,
            "value": value,
            "domain": _legacy_domain(name),
            "path": "/",
            "expires": expires,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        })
    return out


async def _try_quick_enter(page) -> bool:
    """首页若弹 passport 登录框，点「快速进入」走免密记忆登录（逐行抄 refresh.py）"""
    iframe_el = await page.query_selector("#alibaba-login-box")
    if not iframe_el:
        return True  # 无弹窗 → 当作已登录
    frame = await iframe_el.content_frame()
    if not frame:
        return False
    try:
        # 显式 timeout：Playwright 默认 30s 太长，会让整个 refresh 流程卡住
        await frame.wait_for_load_state("domcontentloaded", timeout=5000)
        await page.wait_for_timeout(800)  # iframe 里 Vue/React 组件 mount 时间
        await frame.get_by_text("快速进入", exact=True).first.click(timeout=5000)
        await page.wait_for_selector("#alibaba-login-box", state="hidden", timeout=10000)
        return True
    except Exception as e:  # noqa: BLE001
        # 「快速进入」不可用 = 浏览器免密记忆失效，只能人工扫码（L4）
        print(f"[browser-refresh] 「快速进入」不可用（免密记忆可能失效）：{e}")
        return False


async def _refresh_async(flat: dict[str, str]) -> Optional[dict[str, str]]:
    """Playwright 主流程（抄 _refresh_async，cookie 源换 cookie.txt flat dict）"""
    from playwright.async_api import async_playwright

    headless = os.environ.get("GOOFISH_HEADLESS") == "1"
    # 每次独立 tmp profile（抄 browser.py：避开 Chrome SingletonLock 并发冲突）
    profiles_parent = Path.home() / ".goofish-cli" / "profiles"
    profiles_parent.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(tempfile.mkdtemp(prefix="chrome-", dir=str(profiles_parent)))

    try:
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=headless,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                ],
            )
            try:
                try:
                    await context.add_cookies(_flat_to_playwright(flat))
                except Exception as e:
                    print(f"[browser-refresh] 注入 cookie 失败：{e}")

                page = context.pages[0] if context.pages else await context.new_page()
                # 隐藏 webdriver 特征（抄 browser.py）
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )

                await page.goto(HOME_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)

                # 「快速进入」不可用 → 免密记忆失效，返回 None 让上层走人工（抄参考：
                # 避免返回"只更新 _m_h5_tk 但 session 仍失效"的假成功 cookies）
                if not await _try_quick_enter(page):
                    return None

                # 强鉴权页触发完整 session 下发（cookie2/sgcookie/_tb_token_）
                try:
                    await page.goto(AUTH_PROBE_URL, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass  # 抄参考：goto 异常忽略

                flat_out: dict[str, str] = {}
                for c in await context.cookies():
                    if c.get("name") and c.get("value"):
                        flat_out[c["name"]] = c["value"]
                return flat_out
            finally:
                await context.close()
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def is_enabled() -> bool:
    """开关：env XIANYU_BROWSER_REFRESH=0 关闭（抄 GOOFISH_AUTO_REFRESH_TOKEN 语义）"""
    return os.environ.get("XIANYU_BROWSER_REFRESH", "1") != "0"


def refresh_cookie_via_browser(flat: dict[str, str]) -> Optional[dict[str, str]]:
    """同步入口：Playwright 刷一次 cookie，返回新 flat dict（含必需字段）。

    失败返回 None（调用方保持原 cookie 继续跑 + 告警）。
    注意：默认 headful，会短暂弹出 Chrome 窗口——这是过风控的必要条件。
    """
    missing_dep = None
    try:
        import playwright  # noqa: F401
    except ImportError:
        missing_dep = "playwright 未安装（pip install playwright && playwright install chrome）"
    if missing_dep:
        print(f"[browser-refresh] {missing_dep}")
        return None

    try:
        return asyncio.run(_refresh_async(flat))
    except Exception as e:  # noqa: BLE001 — Chrome 未装/超时等
        print(f"[browser-refresh] Playwright 刷 cookie 失败：{type(e).__name__}: {e}")
        return None


def validate_fresh(flat: dict[str, str]) -> list[str]:
    """校验新 cookie 是否含必需字段（抄 _REQUIRED_FRESH_COOKIES 检查），返回缺失清单"""
    return [k for k in REQUIRED_FRESH_COOKIES if k not in flat]


__all__ = [
    "refresh_cookie_via_browser", "is_enabled", "validate_fresh",
    "REQUIRED_FRESH_COOKIES",
]
