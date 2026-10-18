"""
V17.1 接口续期服务 — 照 zhinianboke cookie_renew_api_service.py 实现思路自己重写

功能：
1. 调 hasLogin.do 续期 sgcookie / tracknick / csg / unb / last_u_xianyu_web 等
2. 调 silentHasLogin.do 续期短登录 Cookie
3. 调 setLoginSettings.do 续期长登录 Cookie（havana_lgc2_77，30 天）
4. 合并三个接口返回的 Set-Cookie 到原始 cookies_str
5. 返回 CookieRenewResult（新 cookies_str + 更新字段 + 业务消息）

【许可证】本模块按 zhinianboke cookie_renew_api_service.py 接口和参数参考自行重写，
        不复制其源代码（zhinianboke 是 AGPL-3.0 禁止商业）。
        所有 URL / 参数 / 头基于公开抓包实证 + zhinianboke README 提示整理。

调用方：xianyu_ws_client._cookie_refresh_loop 每 180s 调一次
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# 接口地址（公开抓包）
_HAS_LOGIN_URL_WEB = "https://passport.goofish.com/newlogin/hasLogin.do"
_HAS_LOGIN_URL = "https://passport.goofish.com/newlogin/silentHasLogin.do"
_SET_LOGIN_SETTINGS_URL = "https://passport.goofish.com/ac/account/setLoginSettings.do"

# 单接口超时（秒）
_REQUEST_TIMEOUT_SECONDS = 20


@dataclass(slots=True)
class CookieRenewResult:
    """续期结果"""
    success: bool = False
    new_cookies_str: str = ""
    updated_cookie_names: list[str] = field(default_factory=list)
    api_message: str = ""
    renew_method: str = "none"  # api / none


def _parse_set_cookie(set_cookie_headers: list[str]) -> dict[str, str]:
    """从 Set-Cookie 响应头列表提取 {name: value} dict

    只取第一个 ';' 之前的 name=value，跳过 Max-Age=0（删除操作）。
    """
    cookies: dict[str, str] = {}
    for header in set_cookie_headers:
        if not header or "=" not in header:
            continue
        # 跳过删除操作（Max-Age=0 或 1970 过期）
        if "Max-Age=0" in header or "1970" in header:
            continue
        # 取第一个 ; 之前的 name=value
        first_segment = header.split(";", 1)[0].strip()
        if "=" not in first_segment:
            continue
        name, value = first_segment.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            cookies[name] = value
    return cookies


def _merge_into_cookies_str(
    original_str: str, new_cookies: dict[str, str]
) -> tuple[str, list[str]]:
    """合并 new_cookies 到 original_str，返回（新 cookie 字符串, 更新字段列表）

    - 已存在的字段被覆盖
    - 不存在的字段被追加
    """
    # 解析原始 cookie 为 dict
    original_dict: dict[str, str] = {}
    for piece in original_str.split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        original_dict[k.strip()] = v.strip()

    updated: list[str] = []
    for name, value in new_cookies.items():
        if original_dict.get(name) != value:
            updated.append(name)
        original_dict[name] = value

    # 拼回 "k=v; k=v" 格式
    new_str = "; ".join(f"{k}={v}" for k, v in original_dict.items())
    return new_str, updated


def _trans_cookies(cookies_str: str) -> dict[str, str]:
    """简化版 cookie 解析（避免依赖 common 模块）"""
    cookies: dict[str, str] = {}
    for piece in cookies_str.split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        cookies[k.strip()] = v.strip()
    return cookies


class CookieRenewService:
    """Cookie 接口续期服务（纯 HTTP，无需浏览器）

    使用方式：
        service = CookieRenewService()
        result = await service.renew(cookies_str)
        if result.success:
            # result.new_cookies_str 是合并后的新 cookie
            ...
    """

    async def renew(self, cookies_str: str) -> CookieRenewResult:
        """执行完整续期流程：hasLogin → silentHasLogin → setLoginSettings

        任一接口返回 Set-Cookie 即合并；最终判断有任一字段更新即视为成功。
        """
        result = CookieRenewResult(new_cookies_str=cookies_str)

        if not cookies_str or not cookies_str.strip():
            result.api_message = "Cookie 为空，跳过续期"
            return result

        all_new_cookies: dict[str, str] = {}

        # 第 1 步：hasLogin.do（Web 端登录态确认）
        web_cookies, web_msg, web_ok = await self._call_has_login_web(cookies_str)
        if web_cookies:
            all_new_cookies.update(web_cookies)
            logger.info(f"Cookie 续期 hasLogin.do 收到 {len(web_cookies)} 个新字段")
        if not web_ok:
            logger.warning(f"Cookie 续期 hasLogin.do 业务失败: {web_msg}")

        # 实时合并，避免后续接口用陈旧 cookie
        if all_new_cookies:
            cookies_str, _ = _merge_into_cookies_str(cookies_str, all_new_cookies)

        # 第 2 步：silentHasLogin.do（短登录续期）
        silent_cookies, silent_msg, silent_ok = await self._call_silent_has_login(
            cookies_str
        )
        if silent_cookies:
            all_new_cookies.update(silent_cookies)
            logger.info(
                f"Cookie 续期 silentHasLogin.do 收到 {len(silent_cookies)} 个新字段"
            )
        if not silent_ok:
            logger.warning(f"Cookie 续期 silentHasLogin.do 业务失败: {silent_msg}")

        if all_new_cookies:
            cookies_str, _ = _merge_into_cookies_str(cookies_str, all_new_cookies)

        # 第 3 步：setLoginSettings.do（长登录续期，30 天 havana_lgc2_77）
        set_cookies = await self._call_set_login_settings(cookies_str)
        if set_cookies:
            all_new_cookies.update(set_cookies)
            logger.info(
                f"Cookie 续期 setLoginSettings.do 收到 {len(set_cookies)} 个新字段"
            )

        # 合并所有更新
        if not all_new_cookies:
            result.api_message = "三个接口均未返回新 Set-Cookie（可能已是最新或 session 已彻底失效）"
            logger.warning(f"Cookie 续期失败：{result.api_message}")
            return result

        new_str, updated_names = _merge_into_cookies_str(cookies_str, all_new_cookies)
        result.success = True
        result.new_cookies_str = new_str
        result.updated_cookie_names = updated_names
        result.renew_method = "api"
        result.api_message = f"接口续期成功：{len(updated_names)} 个字段更新"
        logger.info(
            f"✅ Cookie 接口续期成功，更新字段: {', '.join(updated_names)}"
        )
        return result

    async def _call_has_login_web(
        self, cookies_str: str
    ) -> tuple[dict[str, str], str, bool]:
        """调 hasLogin.do（Web 端），返回 (新 cookies dict, 消息, 是否业务成功)"""
        cookie_dict = _trans_cookies(cookies_str)
        hid = cookie_dict.get("unb", "")
        if not hid:
            return {}, "Cookie 缺 unb", False

        xsrf_token = cookie_dict.get("XSRF-TOKEN", "")
        hsiz = cookie_dict.get("cookie2", "")
        umid_token = cookie_dict.get("_uab_collina", "") or cookie_dict.get("cna", "")
        csrf_token = cookie_dict.get("_tb_token_", "")

        now_ms = str(int(time.time() * 1000))
        rand_suffix = str(random.randint(100000, 999999))
        page_trace_id = f"21504{now_ms}{rand_suffix}"
        rnd_value = str(random.random())

        params = {"appName": "xianyu", "fromSite": "77"}
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN",
            "content-type": "application/x-www-form-urlencoded",
            "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "bx-v": "2.5.31",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            ),
            "referer": (
                f"https://passport.goofish.com/mini_login.htm?lang=zh_cn"
                f"&appName=xianyu&appEntrance=web&styleType=vertical"
                f"&bizParams=&notLoadSsoView=false&notKeepLogin=false"
                f"&isMobile=false&qrCodeFirst=false&stie=77&rnd={rnd_value}"
            ),
            "cookie": cookies_str.replace("\n", "").replace("\r", ""),
        }
        if xsrf_token:
            headers["x-xsrf-token"] = xsrf_token

        post_data = (
            f"hid={hid}"
            f"&ltl=true"
            f"&appName=xianyu"
            f"&appEntrance=web"
            f"&_csrf_token={csrf_token}"
            f"&umidToken={umid_token}"
            f"&hsiz={hsiz}"
            f"&bizParams=taobaoBizLoginFrom%3Dweb%26renderRefer%3Dhttps%253A%252F%252Fwww.goofish.com%252F"
            f"&mainPage=false"
            f"&isMobile=false"
            f"&lang=zh_CN"
            f"&returnUrl="
            f"&fromSite=77"
            f"&isIframe=true"
            f"&documentReferer=https%3A%2F%2Fwww.goofish.com%2F"
            f"&defaultView=hasLogin"
            f"&umidTag=SERVER"
            f"&deviceId="
            f"&pageTraceId={page_trace_id}"
        )

        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
                async with session.post(
                    _HAS_LOGIN_URL_WEB,
                    params=params,
                    data=post_data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
                    allow_redirects=False,
                ) as resp:
                    set_cookie_headers = list(resp.headers.getall("Set-Cookie", []))
                    new_cookies = _parse_set_cookie(set_cookie_headers)
                    text = await resp.text()

                    if resp.status not in (200, 302, 303):
                        return (
                            new_cookies,
                            f"hasLogin HTTP {resp.status}",
                            False,
                        )

                    try:
                        res_json = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        return new_cookies, "hasLogin 返回非 JSON", False

                    content = (
                        res_json.get("content")
                        if isinstance(res_json, dict)
                        else None
                    )
                    ok = bool(content.get("success")) if isinstance(content, dict) else False
                    msg = "hasLogin 业务成功" if ok else (
                        str(content.get("titleMsg", ""))
                        if isinstance(content, dict)
                        else "业务失败"
                    )
                    return new_cookies, msg, ok

        except asyncio.TimeoutError:
            return {}, "hasLogin 超时", False
        except aiohttp.ClientError as e:
            return {}, f"hasLogin 网络错误: {e}", False
        except Exception as e:
            return {}, f"hasLogin 异常: {e}", False

    async def _call_silent_has_login(
        self, cookies_str: str
    ) -> tuple[dict[str, str], str, bool]:
        """调 silentHasLogin.do，返回 (新 cookies dict, 消息, 是否业务成功)"""
        params = {
            "documentReferer": "https://www.goofish.com/",
            "appName": "xianyu",
            "appEntrance": "xianyu_sdkSilent",
            "fromSite": "0",
            "ltl": "true",
        }
        headers = {
            "accept": "*/*",
            "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,ru;q=0.7",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "sec-ch-ua": '"Google Chrome";v="146", "Not=A?Brand";v="8"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Win32"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "Referer": "https://www.goofish.com/",
            "cookie": cookies_str.replace("\n", "").replace("\r", ""),
        }

        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
                async with session.post(
                    _HAS_LOGIN_URL,
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
                    allow_redirects=False,
                ) as resp:
                    set_cookie_headers = list(resp.headers.getall("Set-Cookie", []))
                    new_cookies = _parse_set_cookie(set_cookie_headers)
                    text = await resp.text()

                    if resp.status not in (200, 302, 303):
                        return (
                            new_cookies,
                            f"silentHasLogin HTTP {resp.status}",
                            False,
                        )

                    try:
                        res_json = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        return new_cookies, "silentHasLogin 返回非 JSON", False

                    content = (
                        res_json.get("content")
                        if isinstance(res_json, dict)
                        else None
                    )
                    ok = bool(content.get("success")) if isinstance(content, dict) else False
                    msg = "silentHasLogin 业务成功" if ok else (
                        str(content.get("titleMsg") or "业务失败")
                        if isinstance(content, dict)
                        else "业务失败"
                    )
                    return new_cookies, msg, ok

        except asyncio.TimeoutError:
            return {}, "silentHasLogin 超时", False
        except aiohttp.ClientError as e:
            return {}, f"silentHasLogin 网络错误: {e}", False
        except Exception as e:
            return {}, f"silentHasLogin 异常: {e}", False

    async def _call_set_login_settings(self, cookies_str: str) -> dict[str, str]:
        """调 setLoginSettings.do（长登录续期 30 天），返回新 cookies dict"""
        params = {"fromSite": "77", "appName": "xianyu", "bizEntrance": "web"}
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "content-type": "application/x-www-form-urlencoded",
            "referer": "https://www.goofish.com/",
            "sec-ch-ua": '"Google Chrome";v="146", "Not=A?Brand";v="8"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "cookie": cookies_str.replace("\n", "").replace("\r", ""),
        }

        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
                async with session.post(
                    _SET_LOGIN_SETTINGS_URL,
                    params=params,
                    data="status=0",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
                    allow_redirects=False,
                ) as resp:
                    set_cookie_headers = list(resp.headers.getall("Set-Cookie", []))
                    new_cookies = _parse_set_cookie(set_cookie_headers)
                    if new_cookies:
                        logger.info(
                            f"setLoginSettings.do 长登录续期成功，"
                            f"收到 {len(new_cookies)} 个 cookie: "
                            f"{', '.join(new_cookies.keys())}"
                        )
                    return new_cookies

        except asyncio.TimeoutError:
            logger.warning("setLoginSettings 超时")
            return {}
        except aiohttp.ClientError as e:
            logger.warning(f"setLoginSettings 网络错误: {e}")
            return {}
        except Exception as e:
            logger.warning(f"setLoginSettings 异常: {e}")
            return {}


# 单例
_renew_service_instance: Optional[CookieRenewService] = None


def get_renew_service() -> CookieRenewService:
    global _renew_service_instance
    if _renew_service_instance is None:
        _renew_service_instance = CookieRenewService()
    return _renew_service_instance


__all__ = ["CookieRenewService", "CookieRenewResult", "get_renew_service"]
