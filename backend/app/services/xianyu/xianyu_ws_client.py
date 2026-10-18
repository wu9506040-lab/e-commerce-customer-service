"""
XianyuWsClient — V17 纯 Python 实现（彻底摆脱 goofish-cli + Playwright/Chromium）

设计目标：
- 替代 XianyuWatcher（不再用 goofish-cli subprocess）
- 接口与原 XianyuWatcher 完全一致，daemon 不用改
- Cookie 直接从浏览器 F12 复制（不用扫码、不用 Chromium）
- Cookie 自动续期（每 180s 调 mtop refresh_token，方案 A：失败就提示用户重启 daemon）
- 单文件实现，无额外依赖（除 websockets + requests）

参考（已完整调研 2026-10-17）：
- shaxiu/XianyuAutoAgent (main.py / XianyuApis.py / utils/xianyu_utils.py)
- zhinianboke/xianyu-auto-reply (connection_manager.py / token_manager.py / connection_errors.py)

接口兼容 XianyuWatcher:
    ws = XianyuWsClient(callback=debounce.on_message)
    ws.start()  # 启动（幂等）
    ws.stop()   # 优雅关闭
    ws.is_alive()  # bool

callback(event_dict) 契约（与 DebounceQueue.on_message 对齐）：
    {
        "event": "message",
        "cid": str,            # chat_id（去 @goofish 后缀）
        "text": str,           # 消息文本 ⚠️ 字段名是 "text" 不是 "send_message"
        "send_user_id": str,   # 发送者 unb
        "send_user_name": str,
        "item_id": str,
        "ts": int,             # 创建时间（毫秒）
    }

LWP 协议 (Long-polling WebSocket Protocol, 钉钉体系):
- 端点: wss://wss-goofish.dingtalk.com/
- 注册: /reg (app-key=444e9908..., token=accessToken, ua=DingTalk UA) + /r/SyncStatus/ackDiff
- 心跳: /!  每 15s（应用层）+ ping_interval=20（库自身双层）
- 发消息: /r/MessageSend/sendByReceiverScope（本类不实现，由 reply_scheduler 调 mtop HTTP 发）
- 收消息: LWP JSON 帧，含 syncPushPackage.data[].data（base64+MessagePack/JSON，需 decrypt）
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import random
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("xianyu_ws_client")


# =============================================================
# 常量
# =============================================================
WS_URL = "wss://wss-goofish.dingtalk.com/"

# 心跳（应用层）
HEARTBEAT_INTERVAL_SEC = 15.0
HEARTBEAT_TIMEOUT_SEC = 5.0
# Token 刷新
TOKEN_REFRESH_INTERVAL_SEC = 3600.0
TOKEN_RETRY_INTERVAL_SEC = 300.0
# Cookie 续期（V17 方案 A：每 180s 调 mtop refresh_token）
COOKIE_REFRESH_INTERVAL_SEC = 180.0
# 消息过期（5 分钟）
MESSAGE_EXPIRE_TIME_MS = 300_000

# WebSocket 连接超时（抄 zhinianboke：默认 10s 太短，TLS 抖动会被误判 Token 失效）
WS_OPEN_TIMEOUT_SEC = 30.0
# 库自身心跳（双层心跳：应用层 + 库自身）
WS_PING_INTERVAL_SEC = 20.0
WS_PING_TIMEOUT_SEC = 15.0

# UA（WS header 用短的；/reg 注册用带 DingTalk 标识的）
# V17.1: 升级到 Chrome 139，跟 zhinianboke 一致（im_token_api.py:192-196）
UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
UA_REG = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 "
    "DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) "
    "DingWeb/2.1.5 IMPaaS DingWeb/2.1.5"
)

# mtop URL 模板
MTOP_BASE = "https://h5api.m.goofish.com/h5/"
MTOP_APPKEY = "34839810"           # mtop URL + sign 用
MTOP_BODY_APPKEY = "444e9908a51d1cb236a27862abc769c9"  # mtop body 的 appKey 字段
REG_APPKEY = "444e9908a51d1cb236a27862abc769c9"         # /reg header 的 app-key
MTOP_JSV = "2.7.2"
MTOP_TYPE = "originaljson"
MTOP_ACCOUNT_SITE = "xianyu"
MTOP_SESSION_OPTION = "AutoLoginOnly"

# mtop get_token（拿 accessToken，WebSocket 注册前必须先有）
MTOP_GET_TOKEN_API = "mtop.taobao.idlemessage.pc.login.token"
MTOP_GET_TOKEN_URL = f"{MTOP_BASE}{MTOP_GET_TOKEN_API}/1.0/"

# mtop refresh_token（Cookie 续期核心！）
MTOP_REFRESH_TOKEN_API = "mtop.taobao.idlemessage.pc.loginuser.get"
MTOP_REFRESH_TOKEN_URL = f"{MTOP_BASE}{MTOP_REFRESH_TOKEN_API}/1.0/"

MTOP_BASE_PARAMS = {
    "jsv": MTOP_JSV,
    "appKey": MTOP_APPKEY,
    "v": "1.0",
    "type": MTOP_TYPE,
    "accountSite": MTOP_ACCOUNT_SITE,
    "dataType": "json",
    "timeout": "20000",
    "sessionOption": MTOP_SESSION_OPTION,
    "spm_cnt": "a21ybx.im.0.0",
    "spm_pre": "a21ybx.item.want.1.14ad3da6ALVq3n",
    "log_id": "14ad3da6ALVq3n",
    # ↓↓↓ V17.1: zhinianboke 抓包补全的 4 个参数（解决新版风控假 token 问题）↓↓↓
    "dangerouslySetWindvaneParams": "%5Bobject%20Object%5D",
    "smToken": "token",
    "queryToken": "sm",
    "sm": "sm",
}

# V17.1: zhinianboke 一致的 headers（Chrome 139 指纹 + zh-CN 优先 + cache-control）
MTOP_HEADERS = {
    "Host": "h5api.m.goofish.com",
    "sec-ch-ua-platform": '"Windows"',
    "user-agent": UA_WEB,
    "accept": "application/json",
    "cache-control": "no-cache",
    "content-type": "application/x-www-form-urlencoded",
    "pragma": "no-cache",
    "sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
    "sec-ch-ua-mobile": "?0",
    "origin": "https://www.goofish.com",
    "sec-fetch-site": "same-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
    "referer": "https://www.goofish.com/",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "priority": "u=1, i",
}


# =============================================================
# 工具函数（抄 shaxiu utils/xianyu_utils.py，已验证 100% 正确）
# =============================================================
def trans_cookies(cookies_str: str) -> dict:
    """解析 cookie 字符串为 dict"""
    cookies: dict = {}
    for cookie in cookies_str.split("; "):
        parts = cookie.split("=", 1)
        if len(parts) == 2:
            cookies[parts[0]] = parts[1]
    return cookies


def generate_mid() -> str:
    """LWP mid（雪花 ID，shaxiu 同款：随机数 + 时间戳）"""
    random_part = int(1000 * random.random())
    timestamp = int(time.time() * 1000)
    return f"{random_part}{timestamp} 0"


def generate_uuid() -> str:
    """消息 UUID（shaxiu 同款）"""
    timestamp = int(time.time() * 1000)
    return f"-{timestamp}1"


def generate_device_id(user_id: str) -> str:
    """UUID v4 + user_id 后缀（shaxiu 同款）"""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    result: list = []
    for i in range(36):
        if i in (8, 13, 18, 23):
            result.append("-")
        elif i == 14:
            result.append("4")
        else:
            if i == 19:
                rand_val = int(16 * random.random())
                result.append(chars[(rand_val & 0x3) | 0x8])
            else:
                rand_val = int(16 * random.random())
                result.append(chars[rand_val])
    return "".join(result) + "-" + user_id


def extract_token(cookies_str: str) -> Optional[str]:
    """从 _m_h5_tk cookie 取前半段（sign 用）"""
    cookies = trans_cookies(cookies_str)
    tk = cookies.get("_m_h5_tk", "")
    if not tk:
        return None
    return tk.split("_")[0]


def generate_sign(t: str, token: str, data: str) -> str:
    """mtop sign: MD5(token&t&appKey&data)（已验证 V17 记忆 + shaxiu 一致）"""
    msg = f"{token}&{t}&{MTOP_APPKEY}&{data}"
    md5_hash = hashlib.md5()
    md5_hash.update(msg.encode("utf-8"))
    return md5_hash.hexdigest()


# =============================================================
# MessagePack 解码 + decrypt（抄 shaxiu utils/xianyu_utils.py）
# =============================================================
class MessagePackDecoder:
    """纯 Python MessagePack 解码（shaxiu 实现）"""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.length = len(data)

    def read_byte(self) -> int:
        if self.pos >= self.length:
            raise ValueError("Unexpected end of data")
        byte = self.data[self.pos]
        self.pos += 1
        return byte

    def read_bytes(self, count: int) -> bytes:
        if self.pos + count > self.length:
            raise ValueError("Unexpected end of data")
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result

    def read_uint16(self) -> int:
        return struct.unpack(">H", self.read_bytes(2))[0]

    def read_uint32(self) -> int:
        return struct.unpack(">I", self.read_bytes(4))[0]

    def read_uint64(self) -> int:
        return struct.unpack(">Q", self.read_bytes(8))[0]

    def read_int8(self) -> int:
        return struct.unpack(">b", self.read_bytes(1))[0]

    def read_int16(self) -> int:
        return struct.unpack(">h", self.read_bytes(2))[0]

    def read_int32(self) -> int:
        return struct.unpack(">i", self.read_bytes(4))[0]

    def read_int64(self) -> int:
        return struct.unpack(">q", self.read_bytes(8))[0]

    def read_float32(self) -> float:
        return struct.unpack(">f", self.read_bytes(4))[0]

    def read_float64(self) -> float:
        return struct.unpack(">d", self.read_bytes(8))[0]

    def read_string(self, length: int) -> str:
        return self.read_bytes(length).decode("utf-8")

    def decode_value(self) -> Any:
        if self.pos >= self.length:
            raise ValueError("Unexpected end of data")
        b = self.read_byte()
        # Positive fixint
        if b <= 0x7F:
            return b
        # Fixmap
        if 0x80 <= b <= 0x8F:
            return self.decode_map(b & 0x0F)
        # Fixarray
        if 0x90 <= b <= 0x9F:
            return self.decode_array(b & 0x0F)
        # Fixstr
        if 0xA0 <= b <= 0xBF:
            return self.read_string(b & 0x1F)
        if b == 0xC0:
            return None
        if b == 0xC2:
            return False
        if b == 0xC3:
            return True
        # bin 8/16/32
        if b == 0xC4:
            return self.read_bytes(self.read_byte())
        if b == 0xC5:
            return self.read_bytes(self.read_uint16())
        if b == 0xC6:
            return self.read_bytes(self.read_uint32())
        if b == 0xCA:
            return self.read_float32()
        if b == 0xCB:
            return self.read_float64()
        # uint 8/16/32/64
        if b == 0xCC:
            return self.read_byte()
        if b == 0xCD:
            return self.read_uint16()
        if b == 0xCE:
            return self.read_uint32()
        if b == 0xCF:
            return self.read_uint64()
        # int 8/16/32/64
        if b == 0xD0:
            return self.read_int8()
        if b == 0xD1:
            return self.read_int16()
        if b == 0xD2:
            return self.read_int32()
        if b == 0xD3:
            return self.read_int64()
        # str 8/16/32
        if b == 0xD9:
            return self.read_string(self.read_byte())
        if b == 0xDA:
            return self.read_string(self.read_uint16())
        if b == 0xDB:
            return self.read_string(self.read_uint32())
        # array 16/32
        if b == 0xDC:
            return self.decode_array(self.read_uint16())
        if b == 0xDD:
            return self.decode_array(self.read_uint32())
        # map 16/32
        if b == 0xDE:
            return self.decode_map(self.read_uint16())
        if b == 0xDF:
            return self.decode_map(self.read_uint32())
        # Negative fixint
        if b >= 0xE0:
            return b - 256
        raise ValueError(f"Unknown format byte: 0x{b:02x}")

    def decode_array(self, size: int) -> list:
        return [self.decode_value() for _ in range(size)]

    def decode_map(self, size: int) -> dict:
        result: dict = {}
        for _ in range(size):
            key = self.decode_value()
            value = self.decode_value()
            result[key] = value
        return result

    def decode(self) -> Any:
        try:
            return self.decode_value()
        except Exception:
            return base64.b64encode(self.data).decode("utf-8")


def decrypt(data: str) -> str:
    """解密 syncPushPackage 里的 data 字段（base64 + MessagePack → JSON）"""
    try:
        # 1. base64 解码
        cleaned = "".join(c for c in data if c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
        while len(cleaned) % 4 != 0:
            cleaned += "="
        try:
            decoded_bytes = base64.b64decode(cleaned)
        except Exception as e:
            return json.dumps({"error": f"Base64 decode failed: {e}", "raw_data": data}, ensure_ascii=False)

        # 2. MessagePack → JSON
        try:
            decoder = MessagePackDecoder(decoded_bytes)
            result = decoder.decode()

            def serializer(obj):
                if isinstance(obj, bytes):
                    try:
                        return obj.decode("utf-8")
                    except Exception:
                        return base64.b64encode(obj).decode("utf-8")
                if hasattr(obj, "__dict__"):
                    return obj.__dict__
                return str(obj)

            return json.dumps(result, ensure_ascii=False, default=serializer)
        except Exception:
            try:
                return json.dumps({"text": decoded_bytes.decode("utf-8")}, ensure_ascii=False)
            except Exception:
                return json.dumps({"hex": decoded_bytes.hex()}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Decrypt failed: {e}", "raw_data": data}, ensure_ascii=False)


# =============================================================
# 异常分类（抄 zhinianboke connection_errors.py）
# =============================================================
def is_token_or_session_failure(exc: BaseException) -> bool:
    """判断是否是明确的 Token/Session 失效（→ 不走指数退避，立即刷新 token）"""
    error_text = f"{type(exc).__name__} {exc}".upper()
    return any(marker in error_text for marker in (
        "FAIL_SYS_SESSION_EXPIRED",
        "FAIL_SYS_TOKEN_EXPIRED",
        "FAIL_SYS_TOKEN_EXOIRED",
        "SESSION_EXPIRED",
        "TOKEN_EXPIRED",
        "SESSION IS INVALID",
        "INVALID SESSION",
        "TOKEN IS INVALID",
        "INVALID TOKEN",
    ))


def is_network_connection_error(exc: BaseException) -> bool:
    """判断是否网络错误（→ 走指数退避；含 Windows winerror 121 信号灯超时）"""
    if is_token_or_session_failure(exc):
        return False
    error_type = type(exc).__name__
    error_text = str(exc).lower()
    return (
        "ConnectionClosed" in error_type
        or "ConnectionResetError" in error_type
        or "TimeoutError" in error_type
        or getattr(exc, "winerror", None) == 121
        or any(marker in error_text for marker in (
            "no close frame received or sent",
            "connection reset",
            "winerror 121",
            "信号灯超时时间已到",
            "semaphore timeout period has expired",
        ))
    )


# =============================================================
# Cookie 多源加载（V17 优先级：env > E:\智能客服\cookie.txt > ~/.goofish-cli/cookies.txt）
# =============================================================
def load_cookie_header() -> str:
    """Cookie 多源加载（按优先级）

    1. 环境变量 XIANYU_COOKIE_HEADER (raw "k=v; k=v" 字符串)
    2. E:\\智能客服\\cookie.txt（V17 推荐路径，F12 复制后保存）
    3. ~/.goofish-cli/cookies.txt（兼容旧 goofish-cli）

    Returns:
        Cookie header value（"k=v; k=v" 格式）

    Raises:
        FileNotFoundError: 三个来源都找不到
    """
    env_cookie = os.environ.get("XIANYU_COOKIE_HEADER", "").strip()
    if env_cookie:
        logger.info("Cookie 从环境变量 XIANYU_COOKIE_HEADER 加载")
        return env_cookie

    # V17 推荐路径：E:\智能客服\cookie.txt（用户 F12 复制）
    primary_path = Path("E:/智能客服/cookie.txt")
    if primary_path.exists():
        try:
            text = primary_path.read_text(encoding="utf-8").strip()
            if text:
                logger.info(f"Cookie 从 {primary_path} 加载（{len(text)} chars）")
                return text
        except Exception as e:
            logger.warning(f"读 {primary_path} 失败: {e}")

    # 兼容旧 goofish-cli 路径
    legacy_path = Path.home() / ".goofish-cli" / "cookies.txt"
    if legacy_path.exists():
        try:
            text = legacy_path.read_text(encoding="utf-8").strip()
            if text:
                logger.info(f"Cookie 从 {legacy_path} 加载（兼容旧路径）")
                return text
        except Exception as e:
            logger.warning(f"读 {legacy_path} 失败: {e}")

    raise FileNotFoundError(
        "Cookie 未找到。请在以下任一处配置：\n"
        "  1. 环境变量 XIANYU_COOKIE_HEADER='k=v; k=v'\n"
        "  2. E:\\智能客服\\cookie.txt（V17 推荐，F12 复制后保存）\n"
        "  3. ~/.goofish-cli/cookies.txt（兼容旧 goofish-cli）"
    )


# =============================================================
# mtop HTTP 客户端（requests 同步，抄 shaxiu XianyuApis.py）
# =============================================================
class XianyuApis:
    """mtop HTTP 接口封装（requests.Session 同步）

    用于：
    1. get_token() — 拿 WebSocket 注册用的 accessToken
    2. refresh_token() — Cookie 续期（每 180s 调一次，方案 A 核心）
    """

    def __init__(self, cookies_str: str):
        import requests  # 延迟 import，测试环境可 mock

        self.cookies_str = cookies_str
        self.session = requests.Session()
        self._reload_cookies()

    def _reload_cookies(self):
        """从 self.cookies_str 重新解析 + 注入 session.cookies"""
        cookies = trans_cookies(self.cookies_str)
        self.session.cookies.clear()
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=".goofish.com")

    def refresh_cookies(self, new_cookies_str: str) -> None:
        """热更新 cookie 字符串（用于 reload cookie.txt 后）"""
        self.cookies_str = new_cookies_str
        self._reload_cookies()

    def _post_mtop(self, api_name: str, data_val: str, version: str = "1.0") -> Optional[dict]:
        """调 mtop 接口的通用方法

        V17.1: 处理 Set-Cookie（mtop refresh_token 会返新 _m_h5_tk，必须合并）
        V18: 加 version 参数 —— session.sync 要 v=3.0（probe_mtop_poller 验证过）
        """
        import requests

        url = f"{MTOP_BASE}{api_name}/{version}/"
        params = dict(MTOP_BASE_PARAMS)
        params["api"] = api_name
        params["v"] = version
        params["t"] = ""
        params["sign"] = ""

        res_json: Optional[dict] = None

        # V18 fix（2026-10-17）：token 过期自愈重试。
        # 冷启动时 cookie.txt 里的 _m_h5_tk 可能已过期（daemon 停了 >几分钟），
        # 首次请求必返 FAIL_SYS_TOKEN_EXOIRED（阿里拼写错误，非 EXPIRED）+ Set-Cookie 新 token。
        # 原实现不重试 → 上层死循环连不上 WS。
        # 关键：签名 token 以 session jar 为准（requests 自动吃 Set-Cookie，实测
        # 手动复刻用 jar token 重试立即成功；用 cookies_str 同步值会莫名滞后），
        # cookies_str 只作持久化镜像。
        for _attempt in range(3):
            # V18 fix：t 必须取真实毫秒 int(time.time()*1000) —— 抄 goofish_cli
            # mtop.py 的注释（"int(time.time())*1000 末三位恒为 0"），
            # 部分接口（session.sync 等）对秒级量化 t 直接回 TOKEN_ILLEGAL
            params["t"] = str(int(time.time() * 1000))
            token = None
            try:
                jar_tk_full = self.session.cookies.get("_m_h5_tk") or ""
                if jar_tk_full:
                    token = jar_tk_full.split("_")[0]
            except Exception:
                token = None
            if not token:
                token = extract_token(self.cookies_str)
            if not token:
                logger.error("Cookie 中找不到 _m_h5_tk")
                return None

            params["sign"] = generate_sign(params["t"], token, data_val)

            try:
                response = self.session.post(
                    url,
                    headers=MTOP_HEADERS,
                    params=params,
                    data={"data": data_val},
                    timeout=15,
                )
                res_json = response.json()
            except Exception as e:
                logger.error(f"mtop {api_name} 请求异常: {type(e).__name__}: {e}")
                return None

            # V17.1: 处理 Set-Cookie（mtop refresh_token 通常会返新 _m_h5_tk）
            # 注意：requests.Session 自动处理 Set-Cookie 到 self.session.cookies，
            # 但 self.cookies_str（字符串形式，用于持久化 + 下次签名）需要手动同步
            try:
                if response.cookies:
                    new_set_cookie_names = []
                    for cookie in response.cookies:
                        name = cookie.name
                        value = cookie.value
                        # 把 name=value 加到 self.cookies_str
                        if name and name in trans_cookies(self.cookies_str):
                            # 替换已有字段
                            cookies_dict = trans_cookies(self.cookies_str)
                            if cookies_dict.get(name) != value:
                                cookies_dict[name] = value
                                new_set_cookie_names.append(name)
                        elif name:
                            # 追加新字段
                            cookies_dict = trans_cookies(self.cookies_str)
                            cookies_dict[name] = value
                            new_set_cookie_names.append(name)
                    if new_set_cookie_names:
                        self.cookies_str = "; ".join(
                            f"{k}={v}" for k, v in cookies_dict.items()
                        )
                        logger.info(
                            f"mtop {api_name} 返 Set-Cookie: {', '.join(new_set_cookie_names)}"
                        )
                        # V18 fix：不要 _reload_cookies() 重建 jar——requests 已自动把
                        # Set-Cookie 合入 jar（签名即以 jar 为准）；重建反而会用滞后的
                        # cookies_str 覆盖 jar 新 token，导致重试仍拿旧 token 签名。
            except Exception as cookie_e:
                logger.warning(f"处理 mtop Set-Cookie 失败: {type(cookie_e).__name__}: {cookie_e}")

            if not isinstance(res_json, dict):
                logger.error(f"mtop {api_name} 返回格式异常: {type(res_json).__name__}")
                return None

            # 检测风控
            ret = res_json.get("ret", [])
            ret_str = " ".join(ret) if isinstance(ret, list) else str(ret)

            if "RGV587_ERROR" in ret_str or "被挤爆啦" in ret_str:
                logger.error(f"❌ 触发闲鱼风控 {api_name}: {ret}")
                logger.error(
                    "🔴 用户需在闲鱼网页版-消息页过滑块-重新 F12 抓 cookie，"
                    "覆盖 E:\\智能客服\\cookie.txt 后重启 daemon"
                )
                return None

            # 注意服务端实际返回拼写是 FAIL_SYS_TOKEN_EXOIRED（阿里 mtop 历史拼写错误），
            # 用前缀 FAIL_SYS_TOKEN_EX 同时覆盖 EXOIRED/EXPIRED 两种写法
            if "FAIL_SYS_TOKEN_EX" in ret_str and _attempt == 0:
                # Set-Cookie 已合并进 cookies_str（上方 V17.1 块），重签重试一次
                logger.info(f"mtop {api_name} token 过期，用新 _m_h5_tk 重试一次")
                continue

            if "SUCCESS::调用成功" not in ret_str:
                logger.warning(f"mtop {api_name} 业务失败: ret={ret}")
                return None

            return res_json

        return None

    def get_token(self, device_id: str) -> Optional[dict]:
        """mtop get_token: 拿 WebSocket 注册用的 accessToken

        Returns:
            dict 含 accessToken（如 {"data": {"accessToken": "..."}}），失败 None
        """
        data_val = json.dumps(
            {"appKey": MTOP_BODY_APPKEY, "deviceId": device_id},
            separators=(",", ":"),
        )
        result = self._post_mtop(MTOP_GET_TOKEN_API, data_val)
        if result:
            logger.info("✅ accessToken 获取成功")
        return result

    def refresh_token(self) -> Optional[dict]:
        """Cookie 续期: 调 mtop loginuser.get（V17 方案 A 核心）

        Returns:
            dict（如 {"data": {"accessToken": "..."}}），失败 None
        """
        data_val = "{}"
        result = self._post_mtop(MTOP_REFRESH_TOKEN_API, data_val)
        if result:
            logger.info("✅ Cookie 续期成功")
        return result

    def get_item_info(self, item_id: str) -> Optional[dict]:
        """获取商品详情（V18 抄 XianyuAutoAgent XianyuApis.get_item_info）

        API: mtop.taobao.idle.pc.detail，data={"itemId": "..."}
        Returns:
            dict（res_json，itemDO 在 data.itemDO），失败 None
        """
        data_val = json.dumps({"itemId": str(item_id)}, separators=(",", ":"))
        result = self._post_mtop("mtop.taobao.idle.pc.detail", data_val)
        if result:
            logger.info(f"✅ 商品详情获取成功: {item_id}")
        return result

    def list_sessions(self, fetch_num: int = 20) -> Optional[dict]:
        """拉会话列表（V18 P3 画像数据源，API 经 probe_mtop_poller 验证）

        API: mtop.taobao.idlemessage.pc.session.sync v=3.0，data={"fetchNum": N}
        Returns:
            dict: data.sessions = [{session: {sessionId, userInfo{nick,userId}},
                  message: {summary: {...}}}, ...]，失败 None
        """
        data_val = json.dumps({"fetchNum": int(fetch_num)}, separators=(",", ":"))
        return self._post_mtop(
            "mtop.taobao.idlemessage.pc.session.sync", data_val, version="3.0"
        )

    def get_chat_data(self, cid: str, page_size: int = 10) -> Optional[dict]:
        """拉会话消息历史（V18 P3 画像数据源，API 经 probe_mtop_poller 验证）

        API: mtop.idle.message.individualpage.getchatdata v=1.0
        Returns:
            dict: data.userMessageModels = [...]，失败 None
        """
        data_val = json.dumps(
            {"cid": str(cid), "pageSize": int(page_size), "beginMessageId": ""},
            separators=(",", ":"),
        )
        return self._post_mtop("mtop.idle.message.individualpage.getchatdata", data_val)

    @staticmethod
    def extract_access_token(token_response: Optional[dict]) -> Optional[str]:
        """从 mtop get_token 响应中提取 accessToken（V17.1 严格判空版）

        新版闲鱼风控时 mtop get_token 可能返 'SUCCESS::' 但 accessToken 是空字符串/null，
        拿到后 WebSocket /reg 注册会返 401 uid=null。必须同时满足两个条件：
        1. ret 含 'SUCCESS::调用成功'（避免拿到 ret 失败的"假成功"）
        2. data.accessToken 是非空字符串（避免拿到空 token）
        参考：zhinianboke im_token_api.py:75-97 extract_im_access_token
        """
        if not isinstance(token_response, dict):
            return None
        # 条件 1: ret 必须是 SUCCESS::调用成功
        ret_value = token_response.get("ret", []) or []
        if isinstance(ret_value, str):
            ret_value = [ret_value]
        if not any("SUCCESS::调用成功" in str(item) for item in ret_value):
            return None
        # 条件 2: data.accessToken 必须是非空字符串
        data = token_response.get("data")
        if not isinstance(data, dict):
            return None
        access_token = data.get("accessToken")
        if isinstance(access_token, str) and access_token:
            return access_token
        return None


# =============================================================
# XianyuWsClient — 主类
# =============================================================
class XianyuWsClient:
    """自实现 WebSocket 客户端（替代 XianyuWatcher，V17）

    接口与原 XianyuWatcher 完全一致：
    - __init__(callback, ...)
    - start() — 启动（幂等）
    - stop() — 优雅关闭
    - is_alive() — bool
    - callback(event_dict) — event 格式见模块 docstring

    内部用 threading + asyncio 跑 WebSocket 长连：
    - 主线程 start() → 起 daemon thread
    - daemon thread 内 asyncio.new_event_loop() 跑主循环
    - 主循环 while True: connect → register → 后台任务（心跳/token/cookie）→ 收消息
    - 断开后指数退避 + 抖动重连
    """

    def __init__(
        self,
        callback: Callable[[dict], None],
        cookie_path: Optional[str] = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SEC,
        cookie_refresh_interval: float = COOKIE_REFRESH_INTERVAL_SEC,
        token_refresh_interval: float = TOKEN_REFRESH_INTERVAL_SEC,
    ):
        """
        Args:
            callback: 收到业务消息时调 callback(event_dict)
            cookie_path: 可显式指定 cookie 文件路径；None 则走多源加载
            heartbeat_interval: 应用层心跳间隔（秒）
            cookie_refresh_interval: Cookie 续期间隔（秒，默认 180）
            token_refresh_interval: accessToken 刷新间隔（秒，默认 3600）
        """
        self._callback = callback
        self._heartbeat_interval = heartbeat_interval
        self._cookie_refresh_interval = cookie_refresh_interval
        self._token_refresh_interval = token_refresh_interval

        # 线程模型
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None

        # Stale event 检测（V16.5 兼容）
        self._start_time = time.time()
        self._last_message_event_at = time.time()
        self._last_heartbeat_time = time.time()
        self._last_heartbeat_response = time.time()

        # 重连计数（指数退避用）
        self._retry_count = 0

        # V18: LWP 请求-响应关联（mid 等待队列，识别 CSI_FORBID 等服务端拒绝）
        # 参考 zhinianboke xianyu_async.py:140 _pending_mid_futures: Dict[str, asyncio.Future]
        self._pending_mid_futures: Dict[str, asyncio.Future] = {}

        # 加载 cookie（fail-fast 让用户感知错误）
        if cookie_path:
            cookie_header = Path(cookie_path).read_text(encoding="utf-8").strip()
            logger.info(f"Cookie 从显式路径 {cookie_path} 加载")
        else:
            cookie_header = load_cookie_header()
            # V17.1: cookie_path=None 时，自动用默认路径（让 cookie 续期能写回）
            cookie_path = "E:/智能客服/cookie.txt"
        self._cookie_header = cookie_header
        # V17.1: 保存 cookie_path，cookie 续期时写回
        self._cookie_path = cookie_path

        # 解析 cookie（要 unb + _m_h5_tk）
        cookies = trans_cookies(cookie_header)
        if "unb" not in cookies:
            raise ValueError("Cookie 中缺少必需的 unb 字段")
        self._unb = cookies["unb"]
        self._device_id = generate_device_id(self._unb)

        # mtop HTTP 客户端
        self._apis = XianyuApis(cookie_header)
        self._access_token: Optional[str] = None
        self._last_token_refresh_time = 0.0

        logger.info(
            f"XianyuWsClient 初始化完成 (unb={self._unb}, "
            f"cookie_chars={len(cookie_header)})"
        )

    # ============================================================
    # 公开 API（兼容 XianyuWatcher）
    # ============================================================
    def start(self) -> None:
        """启动 watcher（幂等：重复 start 是 no-op）"""
        if self._thread is not None and self._thread.is_alive():
            logger.info("XianyuWsClient 已在运行, start() 跳过")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_async,
            name="xianyu-ws-client",
            daemon=True,
        )
        self._thread.start()
        logger.info("XianyuWsClient thread 启动")

    def stop(self) -> None:
        """优雅关闭"""
        self._stop_event.set()
        # 主动关 ws（让 async for 退出）
        if self._ws is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("XianyuWsClient 停止")

    def is_alive(self) -> bool:
        """watcher 是否在运行"""
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    # ============================================================
    # 内部：跑 asyncio 主循环（含指数退避重连）
    # ============================================================
    def _run_async(self) -> None:
        """线程入口：asyncio 主循环 + 指数退避重连"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        last_error_msg = ""
        try:
            while not self._stop_event.is_set():
                try:
                    self._loop.run_until_complete(self._connect_and_run())
                except Exception as e:
                    last_error_msg = str(e)
                    logger.warning(
                        f"XianyuWsClient 连接异常: {type(e).__name__}: {e}"
                    )

                if self._stop_event.is_set():
                    break

                # 指数退避 + 抖动（避免多账号同时重连冲击闲鱼）
                self._retry_count += 1
                delay = self._calculate_retry_delay(self._retry_count, last_error_msg)
                logger.info(
                    f"XianyuWsClient 将在 {delay:.1f}s 后重连 "
                    f"(retry_count={self._retry_count})"
                )

                if self._stop_event.wait(timeout=delay):
                    break  # 收到 stop 信号
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None
        logger.info("XianyuWsClient thread 退出")

    def _calculate_retry_delay(self, retry_count: int, error_msg: str) -> float:
        """指数退避 + 抖动（抄 zhinianboke）"""
        err_lower = error_msg.lower() if error_msg else ""
        if "no close frame" in err_lower:
            base = min(2 ** retry_count, 30)  # WS 意外断开，封顶 30s
        elif "connection refused" in err_lower or "timeout" in err_lower:
            base = min(2 * (2 ** retry_count), 90)  # 网络/超时，封顶 90s
        else:
            base = min(2 ** retry_count, 45)  # 通用，封顶 45s
        # 加 0-30% 随机抖动（避免多账号同时重连）
        jitter = random.uniform(0, base * 0.3)
        return round(base + jitter, 1)

    # ============================================================
    # 内部：单次 WebSocket 连接 + 跑
    # ============================================================
    async def _connect_and_run(self) -> None:
        """单次 WebSocket 连接 + 注册 + 后台任务 + 收消息"""
        import websockets

        # 智能选择 header kwarg（websockets ≥ 14 改 additional_headers）
        header_kwarg = self._resolve_header_kwarg(websockets)
        websockets_version = getattr(websockets, "__version__", "未知")
        logger.info(f"websockets 库版本: {websockets_version}, header kwarg: {header_kwarg}")

        headers = {
            "Cookie": self._cookie_header,
            "Host": "wss-goofish.dingtalk.com",
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": UA_WEB,
            "Origin": "https://www.goofish.com",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

        logger.info(f"WebSocket connecting to {WS_URL} ...")
        async with websockets.connect(
            WS_URL,
            **{header_kwarg: headers},
            ping_interval=WS_PING_INTERVAL_SEC,
            ping_timeout=WS_PING_TIMEOUT_SEC,
            open_timeout=WS_OPEN_TIMEOUT_SEC,
            max_size=4 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            logger.info("✅ WebSocket 连接建立成功")
            self._retry_count = 0  # 重置重连计数

            try:
                # 1. 注册（/reg + /r/SyncStatus/ackDiff）
                await self._register(ws)

                # 2. 启动后台任务
                tasks = [
                    asyncio.create_task(self._heartbeat_loop(ws)),
                    asyncio.create_task(self._token_refresh_loop()),
                    asyncio.create_task(self._cookie_refresh_loop()),
                ]

                try:
                    # 3. 收消息循环
                    await self._recv_loop(ws)
                finally:
                    # 关闭后台任务
                    for t in tasks:
                        t.cancel()
                    for t in tasks:
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
            finally:
                self._ws = None

    def _resolve_header_kwarg(self, websockets_module) -> str:
        """智能判断 websockets 版本的 header 参数名（抄 zhinianboke）

        websockets ≥ 14 改 extra_headers → additional_headers
        且新版 connect() 是惰性的，参数错不会立即报错（要在 await 才报）
        """
        try:
            params = inspect.signature(websockets_module.connect).parameters
            if "additional_headers" in params:
                return "additional_headers"
            if "extra_headers" in params:
                return "extra_headers"
        except (ValueError, TypeError):
            pass
        # 默认新版本（保守）
        return "additional_headers"

    # ============================================================
    # /reg 注册（抄 shaxiu main.py::XianyuLive.init）
    # ============================================================
    async def _register(self, ws) -> None:
        """1. 拿 accessToken 2. 发 /reg 注册包 3. 发 SyncStatus/ackDiff"""
        if not self._access_token:
            await self._refresh_access_token()
        if not self._access_token:
            raise RuntimeError("无法获取 accessToken（Cookie 失效或 mtop 接口异常）")

        # /reg 注册
        reg_msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": REG_APPKEY,  # ⚠️ 与 mtop APPKEY 不同！
                "token": self._access_token,
                "ua": UA_REG,  # 带 DingTalk 标识
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self._device_id,
                "mid": generate_mid(),
            },
        }
        await ws.send(json.dumps(reg_msg, ensure_ascii=False))
        await asyncio.sleep(1)

        # /r/SyncStatus/ackDiff（shaxiu 同款 body 结构）
        await self._send_ackdiff(ws, "register")
        logger.info("✅ /reg + SyncStatus/ackDiff 注册完成")

    async def _send_ackdiff(self, ws, reason: str) -> None:
        """V18: 发 SyncStatus/ackDiff — 让服务端补推未同步的消息。

        背景：买家消息有时只推轻量通知（无消息体，解析不出内容），完整内容
        可能 2 分钟后才随大包到达 → 回复延迟数分钟。主动 ackDiff 让服务端
        立即补推。消息有 message_id 去重兜底，重复推送不会重复回复。
        """
        try:
            current_time = int(time.time() * 1000)
            sync_msg = {
                "lwp": "/r/SyncStatus/ackDiff",
                "headers": {"mid": "5701741704675979 0"},
                "body": [{
                    "pipeline": "sync",
                    "tooLong2Tag": "PNM,1",
                    "channel": "sync",
                    "topic": "sync",
                    "highPts": 0,
                    "pts": current_time * 1000,
                    "seq": 0,
                    "timestamp": current_time,
                }],
            }
            await ws.send(json.dumps(sync_msg, ensure_ascii=False))
            logger.info(f"✅ ackDiff 补拉已发送（{reason}）")
        except Exception as e:
            logger.warning(f"ackDiff 发送失败（{reason}）: {type(e).__name__}: {e}")

    def _schedule_resync(self, ws) -> None:
        """V18: 收到轻量通知（无消息体）后安排一次补拉 — 8s 合流 + 2s 延迟。

        2s 延迟是赌一把完整推送随后就到（多数情况如此），到时 ackDiff 拉
        回的多是已处理过的消息，去重兜底。
        """
        now = time.monotonic()
        if now - getattr(self, "_last_resync_at", 0.0) < 8.0:
            return
        self._last_resync_at = now
        try:
            asyncio.get_running_loop().create_task(self._delayed_resync(ws))
        except RuntimeError:
            pass

    async def _delayed_resync(self, ws) -> None:
        await asyncio.sleep(2)
        if self._stop_event.is_set():
            return
        await self._send_ackdiff(ws, "lightweight-notification")

    async def _refresh_access_token(self) -> None:
        """调 mtop get_token 拿新 accessToken（WebSocket 注册前必须）"""
        result = self._apis.get_token(self._device_id)
        if result:
            self._access_token = XianyuApis.extract_access_token(result)
            self._last_token_refresh_time = time.time()
            if self._access_token:
                logger.info(f"✅ accessToken 刷新成功: {self._access_token[:20]}...")
            else:
                logger.warning("⚠️ accessToken 字段缺失")
        else:
            self._access_token = None
            logger.warning("⚠️ accessToken 刷新失败")

    # ============================================================
    # 后台任务：心跳 + Token 刷新 + Cookie 续期
    # ============================================================
    async def _heartbeat_loop(self, ws) -> None:
        """应用层心跳（LWP /!） — 与库自身 ping_interval=20s 互补"""
        _hb_count = 0
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self._heartbeat_interval)
                msg = {"lwp": "/!", "headers": {"mid": generate_mid()}}
                await ws.send(json.dumps(msg, ensure_ascii=False))
                self._last_heartbeat_time = time.time()
                # V18: 每 60s 捎带一次 ackDiff（15s 心跳 × 4）——兜底补拉
                # 任何因轻量通知漏掉的消息，日志上也能当"连接活着"的证据
                _hb_count += 1
                if _hb_count % 4 == 0:
                    await self._send_ackdiff(ws, "periodic-60s")
                logger.debug("应用层心跳已发")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"心跳失败: {type(e).__name__}: {e}")
            raise  # 让外层捕获 → 重连

    async def _token_refresh_loop(self) -> None:
        """Token 刷新: 每 60s 检查一次，过期则刷新（抄 shaxiu）

        Token 变了 → 关 ws 让外层重连（带新 token 重新 /reg）
        """
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(60)
                if time.time() - self._last_token_refresh_time < self._token_refresh_interval:
                    continue

                logger.info("Token 即将过期，准备刷新...")
                old_token = self._access_token
                await self._refresh_access_token()
                if self._access_token and self._access_token != old_token:
                    logger.info("Token 已刷新，重启 ws 带新 token 重新 /reg...")
                    if self._ws:
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
                    return
                else:
                    logger.warning(f"Token 刷新失败, {TOKEN_RETRY_INTERVAL_SEC}s 后重试")
                    await asyncio.sleep(TOKEN_RETRY_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise

    async def _cookie_refresh_loop(self) -> None:
        """Cookie 续期：每 180s 调 passport 接口续期 session + mtop refresh_token

        V17.1: 加 passport 接口续期（hasLogin + silentHasLogin + setLoginSettings）
        解决 mtop send_message FAIL_SYS_SESSION_EXPIRED 问题。
        续期成功后写回 cookie.txt（goofish-cli subprocess 下次启动也用新 cookie），
        并刷新 XianyuApis 内部 requests.Session 的 cookie jar。
        """
        from app.services.xianyu.cookie_renew_service import get_renew_service
        renew_service = get_renew_service()
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self._cookie_refresh_interval)

                # V17.1: 先调 passport 接口续期 session（解决 send_message FAIL_SYS_SESSION_EXPIRED）
                try:
                    renew_result = await renew_service.renew(self._cookie_header)
                    if renew_result.success:
                        logger.info(
                            f"✅ session 接口续期成功: "
                            f"{', '.join(renew_result.updated_cookie_names)}"
                        )
                        self._cookie_header = renew_result.new_cookies_str
                        # 写回 cookie.txt（让 goofish-cli subprocess 下次启动也用新 cookie）
                        if self._cookie_path:
                            try:
                                Path(self._cookie_path).write_text(
                                    self._cookie_header, encoding="utf-8"
                                )
                                logger.info(f"✅ session 已写回 cookie.txt: {self._cookie_path}")
                            except Exception as write_e:
                                logger.warning(f"写回 cookie.txt 失败: {write_e}")
                        # 刷新 XianyuApis 内部 session 的 cookie jar
                        try:
                            self._apis.refresh_cookies(self._cookie_header)
                        except Exception as api_e:
                            logger.warning(f"刷新 XianyuApis cookie 失败: {api_e}")
                    else:
                        logger.warning(
                            f"⚠️ session 接口续期失败: {renew_result.api_message}"
                        )
                except Exception as renew_e:
                    logger.warning(
                        f"session 接口续期异常: {type(renew_e).__name__}: {renew_e}"
                    )

                # 原逻辑：mtop refresh_token（轻量 ping 保 accessToken 有效）
                logger.info("Cookie 续期: 调 mtop refresh_token...")
                cookies_before = self._apis.cookies_str
                result = self._apis.refresh_token()
                cookies_after = self._apis.cookies_str
                if not result:
                    self._renew_fail_streak = getattr(self, "_renew_fail_streak", 0) + 1
                    logger.warning(
                        "⚠️ Cookie 续期失败。可能 Cookie 已失效或触发风控。"
                        "请在闲鱼网页版-消息页过滑块-重新 F12 抓 cookie，"
                        "覆盖 E:\\智能客服\\cookie.txt 后重启 daemon。"
                        f"（连续失败 {self._renew_fail_streak} 次）"
                    )
                    # V18: L3 Playwright 免密刷新（抄 goofish_cli core/refresh.py）——
                    # 连续失败 ≥3 次（≈9 分钟）自动用浏览器「快速进入」免密续期，
                    # 用户无感。ILLEGAL 按参考结论属风控层救不了，但浏览器重新登录
                    # 会下发带正确指纹的完整 session，仍做 best-effort 尝试。
                    await self._maybe_browser_refresh()
                else:
                    self._renew_fail_streak = 0
                    logger.info("✅ Cookie 续期成功")
                    # V17.1: mtop refresh 可能返新 _m_h5_tk 等 Set-Cookie
                    # 如果 _apis.cookies_str 变化了，同步到 daemon 内存 + 写回 cookie.txt
                    if cookies_after != cookies_before:
                        self._cookie_header = cookies_after
                        logger.info(
                            "mtop refresh 返新 Set-Cookie，已同步到 daemon 内存 cookie"
                        )
                        if self._cookie_path:
                            try:
                                Path(self._cookie_path).write_text(
                                    self._cookie_header, encoding="utf-8"
                                )
                                logger.info(
                                    f"✅ mtop 续期新 cookie 已写回: {self._cookie_path}"
                                )
                            except Exception as write_e:
                                logger.warning(f"mtop 写回 cookie.txt 失败: {write_e}")
        except asyncio.CancelledError:
            raise

    async def _maybe_browser_refresh(self) -> None:
        """V18: L3 Playwright 免密刷新触发器（连续续期失败 ≥3 次 + 30min 冷却）"""
        import time as _time
        try:
            from app.services.xianyu import cookie_browser_refresh as cbr
        except Exception:
            return
        if not cbr.is_enabled():
            return
        now = _time.monotonic()
        cooldown = float(os.environ.get("XIANYU_BROWSER_REFRESH_COOLDOWN", "1800"))
        if now - getattr(self, "_last_browser_refresh", 0.0) < cooldown:
            logger.debug("browser refresh 冷却中，跳过")
            return
        self._last_browser_refresh = now

        logger.warning(
            "🔴 续期连续失败，尝试 L3 Playwright 免密刷新"
            "（将短暂弹出 Chrome 窗口，自动点「快速进入」，请勿关闭）..."
        )
        old_flat = trans_cookies(self._apis.cookies_str)
        # 浏览器流程可能跑 30-60s（headful 启动 + 页面加载），放 executor 防阻塞 loop
        new_flat = await asyncio.to_thread(
            cbr.refresh_cookie_via_browser, dict(old_flat)
        )
        if not new_flat:
            logger.warning("L3 浏览器刷新失败（免密记忆可能失效，需人工扫码/F12）")
            return
        missing = cbr.validate_fresh(new_flat)
        if missing:
            logger.warning(f"L3 刷新结果缺必需字段 {missing}，放弃合并（假成功防御，抄参考）")
            return
        # 合并：fresh 覆盖同名，旧字段保留（抄参考 merge 语义，落到 flat 粒度）
        merged = dict(old_flat)
        merged.update(new_flat)
        merged_str = "; ".join(f"{k}={v}" for k, v in merged.items())
        self._cookie_header = merged_str
        try:
            self._apis.refresh_cookies(merged_str)
        except Exception as e:
            logger.warning(f"L3 刷新后同步 apis jar 失败: {e}")
        if self._cookie_path:
            try:
                Path(self._cookie_path).write_text(merged_str, encoding="utf-8")
                logger.info(f"✅ L3 浏览器刷新 cookie 已写回: {self._cookie_path}")
            except Exception as e:
                logger.warning(f"L3 写回 cookie.txt 失败: {e}")
        logger.info("✅ L3 Playwright 免密刷新完成，无需人工干预")

    # ============================================================
    # 收消息循环
    # ============================================================
    async def _recv_loop(self, ws) -> None:
        """持续收消息 → 解析 → callback"""
        async for raw in ws:
            if self._stop_event.is_set():
                break
            # V17 调试：每条原始 WebSocket 帧都 INFO 打印（诊断服务端是否推消息）
            try:
                raw_preview = raw[:30000] if isinstance(raw, (str, bytes)) else str(raw)[:30000]  # V18: 2000→30000 抓全帧诊断 chatType 新格式
            except Exception:
                raw_preview = "<binary>"
            logger.info(f"[recv] {len(raw) if raw else 0} bytes: {raw_preview!r}")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("非 JSON 帧, 跳过")
                continue

            # 心跳响应检测（headers.mid + code==200 + 无 body）
            if self._is_heartbeat_response(msg):
                self._last_heartbeat_response = time.time()
                logger.debug("心跳响应正常")
                # 心跳响应也要 ack（但不调 _handle_message）

            # 发 ACK（每个收到的 mid 帧都回 code=200）
            try:
                await ws.send(json.dumps(self._build_ack(msg), ensure_ascii=False))
            except Exception as e:
                logger.debug(f"ack 失败: {e}")

            # 解析业务消息（同步方法，在 worker thread 内运行）
            self._handle_message(msg)

    def _is_heartbeat_response(self, msg: dict) -> bool:
        """判断是否是心跳响应（code=200 + headers.mid + 无 body）"""
        try:
            if not isinstance(msg, dict):
                return False
            headers = msg.get("headers", {})
            if not isinstance(headers, dict):
                return False
            if "mid" not in headers:
                return False
            if msg.get("code") != 200:
                return False
            if msg.get("body"):
                return False
            return True
        except Exception:
            return False

    def _build_ack(self, msg: dict) -> dict:
        """构造 ACK 帧 — 照抄 xianyu-auto-reply message_handler._send_ack

        参考实现（能用的格式）：
            {"code": 200, "headers": {"mid", "sid", "app-key"?, "ua"?, "dt"?}}

        ⚠️ 此前抄错：{"lwp": "/ack", "headers": {"code": 200, ...}} ——
        code 塞进 headers + 多了 lwp 包装 → 服务端不认，推送的消息一直不算
        "已送达"，完整内容延迟几分钟才随大包下来（2026-10-17 消息延迟 3 分钟
        事故的真正根因）。
        """
        headers_in = msg.get("headers", {}) if isinstance(msg, dict) else {}
        if not isinstance(headers_in, dict):
            headers_in = {}
        ack = {
            "code": 200,
            "headers": {
                "mid": headers_in.get("mid", generate_mid()),
                "sid": headers_in.get("sid", ""),
            },
        }
        # 复制部分原始 headers（照抄参考实现）
        for key in ("app-key", "ua", "dt"):
            if key in headers_in:
                ack["headers"][key] = headers_in[key]
        return ack

    # ============================================================
    # 消息分发（抄 shaxiu main.py::XianyuLive.handle_message）
    # ============================================================
    def _handle_message(self, msg: dict) -> None:
        """解析 LWP 消息 → callback(event_dict)

        消息流：
        1. 跳过心跳响应
        2. 找 syncPushPackage
        3. 解密 data（base64 → MessagePack → JSON）
        4. 判断消息类型（系统订单 / 输入态 / 聊天消息）
        5. 5 分钟过期过滤
        6. 卖家本人消息跳过
        7. 构造 event_dict，callback
        """
        # 1. 跳过心跳响应
        if self._is_heartbeat_response(msg):
            return

        # 2. 判断是否是 syncPushPackage
        if not self._is_sync_package(msg):
            return

        # 3. 提取 syncPushPackage.data 列表
        body = msg.get("body")
        sync_data_list = None
        # V17 修复：兼容两种 body 结构
        # 旧协议（shaxiu 风格）：lwp=/p/syncPushPackage/ackPush, body=list[ {syncPushPackage:{...}} ]
        # 新协议（闲鱼实际）：lwp=/s/vulcan, body=dict { syncPushPackage: {...} }
        if isinstance(body, list):
            for item in body:
                if isinstance(item, dict):
                    sp = item.get("syncPushPackage")
                    if isinstance(sp, dict):
                        data = sp.get("data", [])
                        if isinstance(data, list) and data:
                            sync_data_list = data
                            break
        elif isinstance(body, dict):
            sp = body.get("syncPushPackage")
            if isinstance(sp, dict):
                data = sp.get("data", [])
                if isinstance(data, list) and data:
                    sync_data_list = data
        if not sync_data_list:
            return

        # 4. 处理每条 sync 数据
        for sync_data in sync_data_list:
            if not isinstance(sync_data, dict):
                continue
            inner_data = sync_data.get("data")
            if not inner_data or not isinstance(inner_data, str):
                continue

            # V17 调试：dump syncPushPackage 的 inner_data 摘要（看服务端推什么）
            logger.info(f"[syncPush] inner_data preview: {inner_data[:300]!r}")

            # 5. 解密（先尝试 base64+JSON，失败则用 MessagePack）
            try:
                try:
                    decoded = base64.b64decode(inner_data).decode("utf-8")
                    inner_msg = json.loads(decoded)
                except Exception:
                    inner_msg = json.loads(decrypt(inner_data))
            except Exception as e:
                logger.warning(f"消息解密失败: {type(e).__name__}: {e}")
                continue

            # V17 调试：dump 解析后 inner_msg 顶层 keys
            if isinstance(inner_msg, dict):
                logger.info(
                    f"[syncPush] inner_msg keys={list(inner_msg.keys())[:8]} "
                    f"msg1_keys={list(inner_msg.get('1', {}).keys())[:8] if isinstance(inner_msg.get('1'), dict) else None}"
                )

            if not isinstance(inner_msg, dict):
                continue

            # 6. 系统订单消息 → 打 log 跳过
            try:
                red_reminder = inner_msg.get("3", {}).get("redReminder", "")
                if red_reminder in ("等待买家付款", "交易关闭", "等待卖家发货"):
                    logger.info(f"订单状态消息: {red_reminder}（跳过）")
                    continue
            except (KeyError, TypeError, AttributeError):
                pass

            # 7. 聊天消息判定（必须）
            if not self._is_chat_message(inner_msg):
                # V18（2026-10-17）：轻量通知无消息体（inner_msg["1"] 非 dict）—
                # 若指向真实消息（PNM id），安排 ackDiff 补拉完整内容。
                # 实测 14:34 买家两条消息只推了轻量通知，完整内容 2 分钟后才到，
                # 回复延迟 3 分钟。这是消息延迟的真正元凶。
                if not isinstance(inner_msg.get("1"), dict) and "PNM" in str(inner_msg)[:2000]:
                    self._schedule_resync(self._ws)  # V18.3 fix: ws 未定义（F821，运行到此会 NameError）
                logger.debug(f"非聊天消息: {inner_msg}")
                continue

            # 8. 5 分钟过期过滤
            try:
                create_time = int(inner_msg["1"]["5"])
                if (time.time() * 1000 - create_time) > MESSAGE_EXPIRE_TIME_MS:
                    logger.debug(
                        f"过期消息 ({int((time.time()*1000 - create_time)/1000)}s ago)，跳过"
                    )
                    continue
            except (KeyError, TypeError, ValueError):
                pass

            # 9. 构造 event dict
            ev = self._build_chat_event(inner_msg)
            if not ev:
                continue

            # 10. 卖家本人消息 → 交 seller_callback（V18 人工接管切换），
            # 不进 debounce/自动回复
            if ev["send_user_id"] == self._unb:
                if getattr(self, "_seller_callback", None):
                    try:
                        self._seller_callback(ev)
                    except Exception as e:
                        logger.warning(f"seller_callback 异常: {type(e).__name__}: {e}")
                logger.debug("卖家本人消息，跳过 callback")
                continue

            # 11. callback
            self._last_message_event_at = time.time()
            try:
                self._callback(ev)
            except Exception as e:
                logger.exception(f"callback 异常: {type(e).__name__}: {e}")

    def _is_sync_package(self, msg: dict) -> bool:
        """判断是否是 syncPushPackage 包（兼容旧版 /p/syncPushPackage/ackPush 和新版 /s/vulcan）"""
        try:
            if not isinstance(msg, dict):
                return False
            body = msg.get("body")
            # 旧协议: body=list[ {syncPushPackage: {...}} ]
            if isinstance(body, list):
                for item in body:
                    if isinstance(item, dict) and "syncPushPackage" in item:
                        return True
                return False
            # 新协议: body=dict { syncPushPackage: {...} }
            if isinstance(body, dict) and "syncPushPackage" in body:
                return True
            return False
        except Exception:
            return False

    def _is_chat_message(self, message: dict) -> bool:
        """判断是否是聊天消息（shaxiu 同款判定）"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], dict)
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)
                and "reminderContent" in message["1"]["10"]
            )
        except Exception:
            return False

    def _build_chat_event(self, inner_msg: dict) -> Optional[dict]:
        """从解析后的 sync 消息构造 callback event dict"""
        try:
            msg1_10 = inner_msg["1"]["10"]
            send_user_id = str(msg1_10.get("senderUserId", ""))
            send_user_name = str(msg1_10.get("reminderTitle", ""))
            text = str(msg1_10.get("reminderContent", ""))
            create_time = int(inner_msg["1"]["5"])

            # V17 修复：reminderContent 为空时，从 ["1"]["6"]["3"]["5"] 或 ["1"]["6"]["3"]["1"] 提取
            # （zhinianboke xianyu_message_parser.py::parse_content_payloads 模式）
            if not text:
                text = self._extract_text_from_payload(inner_msg)

            # 解析 item_id（从 reminderUrl）
            reminder_url = msg1_10.get("reminderUrl", "") or ""
            item_id = ""
            if "itemId=" in reminder_url:
                item_id = reminder_url.split("itemId=")[1].split("&")[0]

            # chat_id（去 @goofish 后缀）
            chat_id_raw = inner_msg["1"]["2"]
            chat_id = (
                chat_id_raw.split("@")[0]
                if isinstance(chat_id_raw, str)
                else str(chat_id_raw)
            )

            # ⚠️ 字段名契约（同时输出两个字段，兼容旧 reply_scheduler + 新 DebounceQueue）
            # V18 fix（2026-10-17）：补 message_id —— 此前事件没有 id，
            # reply_scheduler._mark_seen 拿到空串直接 return，seen 集合永远为空，
            # 去重完全失效 → 闲鱼重推（重连/ackDiff）同一批消息会重复回复
            # （实测 12:32-12:33 "已为您转接专员" 连发两次）。
            # 用 cid+ts+text 组合键：服务端重推的同一消息 create_time/text 稳定，
            # 买家真正重发消息时 ts 必然不同，不会误杀。
            return {
                "event": "message",
                "cid": chat_id,
                "message_id": f"{chat_id}_{create_time}_{text[:50]}",
                "text": text,                      # DebounceQueue.on_message 契约
                "send_message": text,              # reply_scheduler._send_message 契约
                "send_user_id": send_user_id,
                "send_user_name": send_user_name,
                "item_id": item_id,
                "ts": create_time,
            }
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"消息字段缺失: {e}")
            return None

    def _extract_text_from_payload(self, inner_msg: dict) -> str:
        """从 ["1"]["6"]["3"]["5"] 或 ["1"]["6"]["3"]["1"] 提取真实文本
        （zhinianboke xianyu_message_parser.py::parse_content_payloads 模式）

        IM 实时推送：载荷是 msgpack 解码后的 dict（已含 contentType/text 字段）
        旧格式/HTTP：载荷是明文 JSON 字符串（先 json.loads 再解析）
        """
        try:
            msg1 = inner_msg.get("1", {})
            if not isinstance(msg1, dict):
                return ""
            msg1_6 = msg1.get("6", {})
            msg1_6_3 = msg1_6.get("3", {}) if isinstance(msg1_6, dict) else {}
            if not isinstance(msg1_6_3, dict):
                return ""

            # V17 调试：dump 真实结构
            logger.info(
                f"[_extract_text] msg1_6_3 type={type(msg1_6_3).__name__} "
                f"keys={list(msg1_6_3.keys())[:8] if isinstance(msg1_6_3, dict) else 'N/A'}"
            )

            # 候选载荷：["6"]["3"]["5"] 优先，["6"]["3"]["1"] 兜底（zhinianboke 模式）
            for candidate_key in ("5", "1"):
                payload = msg1_6_3.get(candidate_key)
                logger.info(
                    f"[_extract_text] candidate['{candidate_key}'] type={type(payload).__name__} "
                    f"preview={str(payload)[:200]!r}"
                )
                if payload is None or payload == "":
                    continue

                # 路径 1：明文 JSON 字符串（HTTP 历史/旧格式）
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue

                # 路径 2：msgpack 解码后已经是 dict
                if isinstance(payload, dict):
                    text = self._extract_text_from_content(payload)
                    if text:
                        return text
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"[_extract_text] 异常: {e}")
        return ""

    @staticmethod
    def _extract_text_from_content(content: dict) -> str:
        """从内容 dict 提取真实文本（zhinianboke _extract_text 模式）"""
        # contentType==1 且含 text → 文本消息
        content_type = content.get("contentType", 0)
        if content_type == 1 and "text" in content:
            text_obj = content["text"]
            if isinstance(text_obj, dict):
                return str(text_obj.get("text", ""))
            return str(text_obj)
        # 兜底：有 text 字段就当文本
        if "text" in content:
            text_obj = content["text"]
            if isinstance(text_obj, dict):
                return str(text_obj.get("text", ""))
            return str(text_obj)
        # 图片
        if content_type == 2:
            return "[图片]"
        return ""

    # =============================================================
    # V18: 发送消息（不走 goofish-cli，直接 WebSocket LWP）
    # 参考 zhinianboke xianyu_async.py:1990-2073 + shaxiu main.py:112-156
    # =============================================================

    def send_text_message_sync(
        self,
        cid: str,
        toid: str,
        text: str,
        timeout: float = 10.0,
    ) -> dict:
        """同步接口：reply_scheduler 主线程调用，跨线程调度到 ws 的 asyncio loop 发消息

        Returns:
            dict: {"ok": bool, "mid": str, "error_type": str?, "message": str?}
        """
        if not self._loop or self._ws is None:
            return {
                "ok": False,
                "error_type": "ws_not_ready",
                "message": "WebSocket 未连接",
            }
        try:
            coro = self._send_text_message_async(cid, toid, text)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            result = future.result(timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return {"ok": False, "error_type": "timeout", "message": "send timeout"}
        except Exception as e:
            return {
                "ok": False,
                "error_type": type(e).__name__,
                "message": str(e),
            }

    async def _send_text_message_async(
        self, cid: str, toid: str, text: str
    ) -> dict:
        """异步实现：在 ws 的 asyncio loop 上发 /r/MessageSend/sendByReceiverScope

        参考 zhinianboke xianyu_async.py:1990-2073：
        - 文本 base64 编码（contentType=1 → custom.contentType=101）
        - mid 雪花 ID + uuid
        - actualReceivers 含 buyer + seller
        - 用 _pending_mid_futures 关联 mid 和服务端响应（识别 CSI_FORBID 安全拦截）
        """
        try:
            # 1. 构建消息内容（text → base64）
            msg_content = {"contentType": 1, "text": {"text": text}}
            content_json = json.dumps(msg_content, ensure_ascii=False)
            content_base64 = base64.b64encode(content_json.encode("utf-8")).decode("utf-8")

            # 2. 生成 mid（雪花 ID）
            mid = generate_mid()

            # 3. 构建 LWP 包（zhinianboke 模板）
            msg = {
                "lwp": "/r/MessageSend/sendByReceiverScope",
                "headers": {"mid": mid},
                "body": [
                    {
                        "uuid": generate_uuid(),
                        "cid": f"{cid}@goofish",
                        "conversationType": 1,
                        "content": {
                            "contentType": 101,
                            "custom": {"type": 1, "data": content_base64},
                        },
                        "redPointPolicy": 0,
                        "extension": {"extJson": "{}"},
                        "ctx": {"appVersion": "1.0", "platform": "web"},
                        "mtags": {},
                        "msgReadStatusSetting": 1,
                    },
                    {
                        "actualReceivers": [
                            f"{toid}@goofish",
                            f"{self._unb}@goofish",
                        ]
                    },
                ],
            }

            # 4. 注册 mid 等待队列（识别服务端响应，含 CSI_FORBID 等）
            send_future = None
            try:
                loop = asyncio.get_running_loop()
                send_future = loop.create_future()
                self._pending_mid_futures[mid] = send_future
            except RuntimeError:
                pass

            # 5. 通过 ws 发消息
            if self._ws is None:
                return {"ok": False, "error_type": "ws_disconnected", "message": "ws 已断开"}
            await self._ws.send(json.dumps(msg, ensure_ascii=False))
            logger.info(
                f"✅ V18 ws 发送消息成功: mid={mid} cid={cid} text={text[:50]!r}"
            )

            # 6. 等服务端响应（最多等 3s，不阻塞太久）
            if send_future is not None:
                try:
                    response = await asyncio.wait_for(send_future, timeout=3.0)
                    # 检查 CSI_FORBID 等安全拦截
                    if isinstance(response, dict) and response.get("code", 200) != 200:
                        logger.warning(f"⚠️ ws send 服务端拒绝: {response}")
                        return {
                            "ok": False,
                            "error_type": "server_rejected",
                            "message": str(response),
                            "mid": mid,
                        }
                    return {"ok": True, "mid": mid, "response": response}
                except asyncio.TimeoutError:
                    # 3s 内没收到 mid 响应也算成功（消息已发，响应可选）
                    logger.debug(f"ws send mid={mid} 未收到服务端 ack，消息已发送")
                    return {"ok": True, "mid": mid}

            return {"ok": True, "mid": mid}
        except Exception as e:
            logger.exception(f"V18 ws 发消息失败: {type(e).__name__}: {e}")
            return {
                "ok": False,
                "error_type": type(e).__name__,
                "message": str(e),
            }


# =============================================================
# CLI selftest（直接跑看连上/能收消息）
# =============================================================
def _selftest() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    events: list = []

    def cb(ev):
        events.append(ev)
        print(f"=== EVENT: {ev}", flush=True)

    c = XianyuWsClient(callback=cb)
    c.start()
    try:
        time.sleep(60)  # 等 60s 看是否连上 + 收消息
    finally:
        c.stop()
    print(f"=== 共 {len(events)} 个事件 ===")
    return 0


if __name__ == "__main__":
    import sys as _s

    _s.exit(_selftest())