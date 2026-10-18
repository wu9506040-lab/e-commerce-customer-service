"""xianyu_token.py - V16.8 P3 闲鱼 token 自动续期

借鉴 goofish_cli core/token.py + shaxiu XianyuAutoAgent:
- accessToken: 30 分钟缓存 (mtop.taobao.idlemessage.pc.login.token)
- refresh_login: 10 分钟轻量 ping (mtop.taobao.idlemessage.pc.loginuser.get)
- mtop 内部自动续 Web cookie (_m_h5_tk 过期时触发 Playwright fallback)

API 字符串兼容 goofish-cli（直接用其常量）
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("xianyu_token")

# mtop key (from goofish-cli core/token.py line 19)
IM_APP_KEY = "34839810"  # shaxiu zhinianboke goofish-cli 真实值 (core/mtop.py APP_KEY)
MTOP_BASE = "https://h5api.m.goofish.com"
TOKEN_CACHE_PATH = Path.home() / ".goofish-cli" / "im_token.json"
DEFAULT_TTL = 30 * 60  # 30 分钟

# 用于 mtop 请求的固定 headers (从 goofish_cli core/mtop.py::default_headers 复制, 避免 server 拒)
MTOP_HEADERS = {
    "accept": "application/json",
    "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
    "cache-control": "no-cache",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://www.goofish.com",
    "pragma": "no-cache",
    "priority": "u=1, i",
    "referer": "https://www.goofish.com/",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    ),
}


class MtopError(Exception):
    pass


class XianyuTokenManager:
    """accessToken 缓存 + 续期 (替代 goofish-cli core/token.py)"""

    def __init__(self, cookie_header: str, unb: str):
        self._cookie_header = cookie_header
        self._unb = unb
        self._token: str | None = None
        self._token_obtained_at: float = 0.0
        # 读 env override
        env = os.environ.get("GOOFISH_IM_TOKEN", "").strip()
        if env:
            self._token = env
            self._token_obtained_at = time.time()  # 永不过期
        else:
            self._load_cache()

    def _ttl(self) -> int:
        try:
            return max(60, int(os.environ.get("GOOFISH_TOKEN_TTL", DEFAULT_TTL)))
        except ValueError:
            return DEFAULT_TTL

    def _load_cache(self) -> None:
        """读 ~/.goofish-cli/im_token.json 缓存"""
        if not TOKEN_CACHE_PATH.exists():
            return
        try:
            raw = json.loads(TOKEN_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if raw.get("unb") != self._unb:
            return
        if time.time() - float(raw.get("t", 0)) > self._ttl():
            return
        self._token = raw.get("token")
        if self._token:
            self._token_obtained_at = float(raw.get("t", 0))
            logger.debug(f"accessToken 命中缓存 (age={(time.time() - self._token_obtained_at):.0f}s)")

    def _save_cache(self, token: str) -> None:
        try:
            TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_CACHE_PATH.write_text(json.dumps({
                "unb": self._unb, "token": token, "t": time.time(),
            }))
            TOKEN_CACHE_PATH.chmod(0o600)
        except OSError as e:
            logger.warning(f"token 缓存写入失败: {e}")

    def _call_login_token_mtop(self) -> str:
        """直接调 mtop API 拿 accessToken

        V16.8 P3 sign 算法: MD5(`${_m_h5_tk}&${t}&${app_key}&${data}`)
        来源:
        - shaxiu/XianyuAutoAgent main.py (utils/xianyu_utils.py:61-69)
        - zhinianboke/xianyu-auto-reply common/utils/xianyu_utils.py:116-131
        - goofish-cli core/sign.py (Go 二进制调用, 算法同)
        """
        import hashlib
        data = {"appKey": IM_APP_KEY, "deviceId": f"xianyu-ws-client-{self._unb[:8]}"}
        mh5tk = self._extract_mh5tk()
        if not mh5tk:
            raise MtopError("cookie 缺 _m_h5_tk (mtop sign 需要)")
        t_ms = str(int(time.time() * 1000))
        data_val = json.dumps(data, separators=(",", ":"))
        sign = hashlib.md5(
            f"{mh5tk}&{t_ms}&{IM_APP_KEY}&{data_val}".encode("utf-8")
        ).hexdigest()
        params = {
            "jsv": "2.7.2",
            "appKey": IM_APP_KEY,
            "t": t_ms,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.taobao.idlemessage.pc.login.token",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.home.0.0",
        }
        url = f"{MTOP_BASE}/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
        resp = requests.post(
            url,
            params=params,
            data={"data": data_val},
            headers={**MTOP_HEADERS, "Cookie": self._cookie_header},
            timeout=30,
        )
        raw = resp.json()
        if "FAIL_SYS_SESSION_EXPIRED" in str(raw) or "FAIL_SYS_TOKEN_EXOIRED" in str(raw):
            raise MtopError(f"session/token 过期: {raw}")
        ret = raw.get("ret", [])
        if "SUCCESS" not in str(ret):
            raise MtopError(f"mtop 失败: ret={raw.get('ret')}")
        token = (raw.get("data") or {}).get("accessToken", "")
        if not token:
            raise MtopError(f"accessToken 缺失: {raw}")
        return token

    def _extract_mh5tk(self) -> str:
        """从 cookie 字符串提取 _m_h5_tk 值"""
        for piece in self._cookie_header.split(";"):
            piece = piece.strip()
            if piece.startswith("_m_h5_tk="):
                return piece[len("_m_h5_tk="):]
        return ""

    def get_token(self) -> str:
        """返回有效 accessToken (优先 env > 缓存 > mtop 调用)"""
        if self._token and not self._is_expired():
            return self._token
        if os.environ.get("GOOFISH_IM_TOKEN", "").strip():
            # env 不失效
            return self._token
        # 缓存失效或没有 → 重新 mtop 调用
        logger.info("accessToken 失效, 调用 mtop.taobao.idlemessage.pc.login.token 续期")
        self._token = self._call_login_token_mtop()
        self._token_obtained_at = time.time()
        self._save_cache(self._token)
        return self._token

    def _is_expired(self) -> bool:
        if not self._token:
            return True
        # env override 不失效
        if os.environ.get("GOOFISH_IM_TOKEN", "").strip():
            return False
        return (time.time() - self._token_obtained_at) > self._ttl()

    def refresh_login(self) -> dict:
        """10 分钟一次轻量 ping 保 Web cookie 有效 (mtop.taobao.idlemessage.pc.loginuser.get)

        V16.8 P3 sign 算法: 同 _call_login_token_mtop
        """
        import hashlib
        data: dict[str, Any] = {}
        mh5tk = self._extract_mh5tk()
        if not mh5tk:
            return {"error": "no _m_h5_tk"}
        t_ms = str(int(time.time() * 1000))
        data_val = json.dumps(data, separators=(",", ":"))
        sign = hashlib.md5(
            f"{mh5tk}&{t_ms}&{IM_APP_KEY}&{data_val}".encode("utf-8")
        ).hexdigest()
        params = {
            "jsv": "2.7.2",
            "appKey": IM_APP_KEY,
            "t": t_ms,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.taobao.idlemessage.pc.loginuser.get",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.home.0.0",
        }
        url = f"{MTOP_BASE}/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/"
        try:
            resp = requests.post(
                url,
                params=params,
                data={"data": data_val},
                headers={**MTOP_HEADERS, "Cookie": self._cookie_header},
                timeout=15,
            )
            return resp.json()
        except Exception as e:
            logger.warning(f"refresh_login 失败: {e}")
            return {"error": str(e)}


async def token_refresh_loop(token_mgr: XianyuTokenManager, interval_sec: int = 600):
    """每 10 分钟 refresh_login + 30 分钟 accessToken 续期 (后台 task)"""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            # 触发 refresh_login (轻量)
            token_mgr.refresh_login()
            logger.debug("refresh_login OK")
        except Exception as e:
            logger.warning(f"refresh_login 异常: {e}")
