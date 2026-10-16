"""
XianyuWsClient — V16.8 P3 借鉴 shaxiu/XianyuAutoAgent 重写 WebSocket 客户端

设计目标：
- 替代 XianyuWatcher（不再用 goofish-cli subprocess，告别 Playwright/Chromium 跨平台问题）
- 接口与原 XianyuWatcher 完全一致，daemon 不用改
- Cookie 多源加载（环境变量 / .txt / .json）
- 借鉴 shaxiu 设计：websockets 库 + LWP 协议 + 心跳 + 自动重连

LWP 协议 (Long-polling WebSocket Protocol, 钉钉体系, JSON 而非 Protobuf):
- 端点: wss://wss-goofish.dingtalk.com/
- 心跳: /!  每 15s
- 注册: /reg (带 accessToken) + /r/SyncStatus/ackDiff
- 发消息: /r/MessageSend/sendByReceiverScope
- 收消息: /push  (JSON frames 含 syncPushPackage.data[].data)

参考:
- shaxiu/XianyuAutoAgent (8.3k stars) main.py - XianyuLive class
- goofish_cli/core/ws.py - LWP 协议 send_text_frame / recv_ack

接口兼容 XianyuWatcher:
  __init__(callback) / start() / stop() / is_alive() / callback event dict
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

# LWP 协议常量
WS_URL = "wss://wss-goofish.dingtalk.com/"
HEARTBEAT_INTERVAL_SEC = 15.0
RECONNECT_DELAY_SEC = 5.0

UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
ORIGIN = "https://www.goofish.com"

logger = logging.getLogger("xianyu_ws_client")


def _generate_mid() -> str:
    """LWP 帧的 mid（消息唯一 ID），用 uuid4"""
    return uuid.uuid4().hex


def _load_cookie_header() -> str:
    """Cookie 多源加载（按优先级）：
    1. 环境变量 XIANYU_COOKIE_HEADER (raw "k=v; k=v" 字符串)
    2. ~/.goofish-cli/cookies.txt (raw 字符串)
    3. ~/.goofish-cli/cookies.json (JSON 数组 [{name, value, domain, path}])

    Returns:
        Cookie 头的 value 部分（直接放进 HTTP "Cookie: ..." 头）
    """
    env_cookie = os.environ.get("XIANYU_COOKIE_HEADER", "").strip()
    if env_cookie:
        return env_cookie

    home = Path.home()
    txt_path = home / ".goofish-cli" / "cookies.txt"
    if txt_path.exists():
        text = txt_path.read_text(encoding="utf-8").strip()
        if text:
            return text

    json_path = home / ".goofish-cli" / "cookies.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"cookies.json 解析失败: {e}")
        if isinstance(data, list) and data:
            return "; ".join(
                f"{c['name']}={c['value']}" for c in data
                if c.get("name") and c.get("value")
            )
        if isinstance(data, dict) and data.get("cookies"):
            return "; ".join(
                f"{c['name']}={c['value']}" for c in data["cookies"]
                if c.get("name") and c.get("value")
            )

    raise FileNotFoundError(
        "Cookie 未找到。请在以下任一处配置：\n"
        "  1. 环境变量 XIANYU_COOKIE_HEADER='k=v; k=v'\n"
        "  2. ~/.goofish-cli/cookies.txt\n"
        "  3. ~/.goofish-cli/cookies.json"
    )


class XianyuWsClient:
    """自实现 WebSocket 客户端（替代 XianyuWatcher）

    启动：start() 启 _thread，_thread 内 asyncio 跑 _connect_and_run
    停止：stop() 设 _stop_event，_thread 退出
    """

    def __init__(
        self,
        callback: Callable[[dict], None],
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SEC,
        reconnect_delay: float = RECONNECT_DELAY_SEC,
    ):
        self._callback = callback
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_delay = reconnect_delay
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None  # type: ignore[assignment]  # websockets.WebSocketClientProtocol
        # P1-H: 启动即开始 stale 检测
        self._start_time = time.time()
        self._last_message_event_at = time.time()
        # 启动前 load cookie（fail fast 让用户感知）
        self._cookie_header = _load_cookie_header()
        logger.info(
            f"XianyuWsClient 初始化 OK (cookie 长度 {len(self._cookie_header)} chars)"
        )

    # ============================================================
    # 公开 API (兼容 XianyuWatcher)
    # ============================================================
    def start(self) -> None:
        if self._thread is not None:
            logger.warning("XianyuWsClient 已启动, start() 跳过")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_async, name="xianyu-ws-client", daemon=True
        )
        self._thread.start()
        logger.info("XianyuWsClient thread 启动")

    def stop(self) -> None:
        self._stop_event.set()
        # 关 ws（可能线程还在 await）
        if self._ws is not None:
            try:
                loop = self._thread_ws_loop
                if loop and not loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        self._ws.close(), loop
                    )
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("XianyuWsClient 停止")

    def is_alive(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and not self._stop_event.is_set()
        )

    # ============================================================
    # 内部: 跑 asyncio event loop
    # ============================================================
    def _run_async(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._thread_ws_loop = loop
        try:
            while not self._stop_event.is_set():
                try:
                    loop.run_until_complete(self._connect_and_run())
                except Exception as e:
                    logger.warning(
                        f"XianyuWsClient 连接异常 (will reconnect in {self._reconnect_delay}s): "
                        f"{type(e).__name__}: {e}"
                    )
                if not self._stop_event.is_set():
                    # 重连前 sleep（stop_event 可打断）
                    self._stop_event.wait(timeout=self._reconnect_delay)
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._thread_ws_loop = None
        logger.info("XianyuWsClient thread 退出")

    # ============================================================
    # 内部: 单次连接 + 跑
    # ============================================================
    async def _connect_and_run(self) -> None:
        import websockets  # 延迟 import, 测试环境不一定装
        headers = {
            "Cookie": self._cookie_header,
            "Host": "wss-goofish.dingtalk.com",
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": UA_WEB,
            "Origin": ORIGIN,
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        logger.info(f"XianyuWsClient connecting to {WS_URL} ...")
        # ping_interval=None: 关 WS ping, 我们走 LWP /! 心跳
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=None,
            max_size=4 * 1024 * 1024,
            open_timeout=15,
        ) as ws:
            self._ws = ws
            logger.info("XianyuWsClient connected")
            # 1. 注册（带 accessToken / /r/SyncStatus/ackDiff）
            await self._send_register(ws)
            # 2. 启动心跳
            hb_task = asyncio.create_task(self._heartbeat_loop(ws))
            # 3. 收消息循环（同步）
            try:
                await self._recv_loop(ws)
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass

    async def _send_register(self, ws) -> None:
        """/reg + /r/SyncStatus/ackDiff

        走 cookie 自动登录，passport 应该已经认识我们（cookie 有效时）。
        """
        # /reg 注册
        reg_mid = _generate_mid()
        await ws.send(json.dumps({
            "lwp": "/reg",
            "headers": {"mid": reg_mid},
            "body": [
                {
                    "appKey": "34839810",  # 跟 mtop 一致（goofish-cli core/mtop.py）
                    "deviceId": f"xianyu-ws-client-{uuid.uuid4().hex[:16]}",
                    "sid": "{}",  # session id, 由 passport 下发
                    "ttid": "1@xianyu_web_pc",
                    "userId": 0,  # 0 = 未登录；passport 下发后填
                    "version": "1.0",
                }
            ],
        }))
        # /r/SyncStatus/ackDiff (passport 用这个确认 ack 状态)
        sync_mid = _generate_mid()
        await ws.send(json.dumps({
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": sync_mid},
            "body": [0, 0],
        }))
        logger.info("register + sync 帧已发")

    async def _heartbeat_loop(self, ws) -> None:
        """/!  每 15s 一次心跳"""
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self._heartbeat_interval)
                mid = _generate_mid()
                await ws.send(json.dumps({
                    "lwp": "/!",
                    "headers": {"mid": mid},
                }))
                logger.debug("心跳已发")
        except Exception as e:
            logger.warning(f"心跳失败: {type(e).__name__}: {e}")
            raise

    # ============================================================
    # 内部: 收消息循环
    # ============================================================
    async def _recv_loop(self, ws) -> None:
        """持续收消息, 解析后 callback"""
        async for raw in ws:
            if self._stop_event.is_set():
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("非 JSON 帧, 跳过")
                continue
            # 收包 ack
            try:
                await ws.send(json.dumps(_build_ack(msg)))
            except Exception as e:
                logger.debug(f"ack 失败: {e}")
            # 处理消息
            self._handle_message(msg)

    def _handle_message(self, msg: dict) -> None:
        """解析 LWP 消息，提取新消息 → 调 callback

        LWP 推消息 frame 通常长这样：
        {
          "lwp": "/p/syncPushPackage/ackPush",
          "headers": {...},
          "body": [{
            "data": [
              {"data": "<encrypted or json>", "messageType": "text"},
              ...
            ]
          }]
        }

        V16.8 P3 第一版：先按 plain JSON 解析（shaxiu 模式）。
        如果服务器真返加密字节（goofish-cli decrypt 流程）—— 后续可加 crypt 解密。
        """
        # 找含 data 的消息
        body = msg.get("body")
        if not body:
            return
        # 1. 找包含 message/messages 字段
        for item in body if isinstance(body, list) else [body]:
            if not isinstance(item, dict):
                continue
            data_arr = (
                item.get("data")
                or item.get("messages")
                or []
            )
            if not isinstance(data_arr, list):
                continue
            for d in data_arr:
                ev = self._parse_one(d)
                if ev:
                    self._last_message_event_at = time.time()
                    try:
                        self._callback(ev)
                    except Exception as e:
                        logger.exception(f"callback 异常: {e}")

    def _parse_one(self, d: dict) -> Optional[dict]:
        """单条消息 → callback event dict

        格式 shaxiu/goofish 通用：
        {
          "cid": "68099209693",
          "sendUserId": "...",
          "sendUserName": "...",
          "message": "...",
          "createTime": 1792123456789,
          "messageType": 1 (text)
        }
        也可能 data 字段是加密字节（goofish 模式），这里 fail-soft
        """
        if not isinstance(d, dict):
            return None
        # 兼容 d.data 嵌套
        inner = d.get("data") if isinstance(d.get("data"), dict) else d
        cid = (
            inner.get("cid")
            or inner.get("sessionId")
            or inner.get("conversationId")
        )
        if not cid:
            return None
        return {
            "event": "message",
            "cid": str(cid),
            "send_user_id": str(
                inner.get("sendUserId")
                or inner.get("fromUserId")
                or ""
            ),
            "send_user_name": str(
                inner.get("sendUserName")
                or inner.get("fromUserName")
                or ""
            ),
            "send_message": str(
                inner.get("message")
                or inner.get("content")
                or inner.get("sendMessage")
                or ""
            ),
            "ts": int(
                inner.get("createTime")
                or inner.get("ts")
                or 0
            ),
        }


# ============================================================
# LWP ack 构造（参考 goofish-cli core/ws.py::build_ack）
# ============================================================
def _build_ack(msg: dict) -> dict:
    """对收到的 mid 帧发 ack（code=200）"""
    return {
        "lwp": "/ack",
        "headers": {
            "code": 200,
            "mid": msg.get("headers", {}).get("mid", _generate_mid()),
        },
    }


# ============================================================
# CLI 调试用: 直接跑看连上/能收消息
# ============================================================
def _selftest() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    events = []

    def cb(ev):
        events.append(ev)
        print(f"=== EVENT: {ev}", flush=True)

    c = XianyuWsClient(callback=cb)
    c.start()
    try:
        time.sleep(30)  # 等 30s 看是否连上 + 收消息
    finally:
        c.stop()
    print(f"=== 共 {len(events)} 个事件 ===")
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(_selftest())
