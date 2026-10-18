"""
AdminBridge — 人工后台 ↔ daemon 桥接（V18 人机协同）

通道设计：
  - 命令下行：Redis Pub/Sub channel `xianyu:commands`
    （API 容器 publish；daemon 订阅执行——daemon 持有唯一的 ws 连接）
  - 事件上行：Redis List `xianyu:events`（daemon LPUSH，API/前端拉取展示）
    事件：buyer_message / reply_sent / takeover / release / send_result

支持命令：
  {"action": "send",     "cid": "...", "toid": "...", "text": "..."}
  {"action": "takeover", "cid": "..."}   # 人工接管（期间 AI 不自动回复）
  {"action": "release",  "cid": "..."}   # 恢复 AI

可靠性：send 命令在独立线程执行（复用 _send_with_retry 无限重试，
不会阻塞订阅循环）；事件 List LTRIM 500 防无限增长。
"""
from __future__ import annotations

import json
import logging
import threading
import time

import redis

logger = logging.getLogger(__name__)

COMMAND_CHANNEL = "xianyu:commands"
EVENTS_KEY = "xianyu:events"
EVENTS_MAX = 500


class AdminBridge:
    """人工后台桥接：订阅命令 + 发布事件（线程安全）"""

    def __init__(
        self,
        redis_url: str,
        on_send,          # callable(cid, toid, text) -> dict（阻塞，内部带重试）
        on_takeover,      # callable(cid) -> None
        on_release,       # callable(cid) -> None
    ):
        self._r = redis.Redis.from_url(redis_url, decode_responses=True)
        self._on_send = on_send
        self._on_takeover = on_takeover
        self._on_release = on_release
        self.toid_map: dict[str, dict] = {}  # cid → {toid, nick, last_text, ts}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.TOID_KEY = "xianyu:toid_map"
        # V18.2: 映射持久化到 Redis（内存重启即丢 → 人工发送缺 toid 事故）
        try:
            saved = self._r.hgetall(self.TOID_KEY)
            for cid, raw in saved.items():
                try:
                    self.toid_map[cid] = json.loads(raw)
                except Exception:
                    pass
            if saved:
                logger.info(f'AdminBridge 恢复 cid→toid 映射 {len(saved)} 条')
        except Exception as e:
            logger.debug(f'加载 toid 映射失败: {e}')

    # ---------------- 事件上行 ----------------

    def publish_event(self, kind: str, data: dict) -> None:
        try:
            payload = json.dumps(
                {"ts": int(time.time() * 1000), "kind": kind, "data": data},
                ensure_ascii=False,
            )
            pipe = self._r.pipeline()
            pipe.lpush(EVENTS_KEY, payload)
            pipe.ltrim(EVENTS_KEY, 0, EVENTS_MAX - 1)
            pipe.execute()
        except Exception as e:
            logger.debug(f"publish_event 失败: {e}")

    def record_buyer_event(self, ev: dict) -> None:
        """ws 回调包装入口：记录 cid→toid 映射 + 发布 buyer_message 事件"""
        try:
            cid = str(ev.get("cid", ""))
            if not cid:
                return
            entry = {
                "toid": str(ev.get("send_user_id", "")),
                "nick": str(ev.get("send_user_name", "") or ""),
                "last_text": str(ev.get("text", ""))[:100],
                "account_id": str(ev.get("account_id", "a1")),
                "ts": int(time.time() * 1000),
            }
            with self._lock:
                self.toid_map[cid] = entry
            # 持久化到 Redis（重启恢复用）
            try:
                self._r.hset(self.TOID_KEY, cid, json.dumps(entry, ensure_ascii=False))
            except Exception:
                pass
            self.publish_event("buyer_message", {
                "cid": cid,
                "nick": str(ev.get("send_user_name", "") or ""),
                "text": str(ev.get("text", ""))[:200],
            })
        except Exception as e:
            logger.debug(f"record_buyer_event 失败: {e}")

    # ---------------- 命令下行 ----------------

    def start(self) -> None:
        t = threading.Thread(target=self._loop, name="admin-bridge", daemon=True)
        t.start()
        logger.info("AdminBridge 订阅启动: channel=xianyu:commands")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pubsub = self._r.pubsub()
                pubsub.subscribe(COMMAND_CHANNEL)
                logger.info("AdminBridge 已连接 Redis 命令通道")
                for msg in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if msg.get("type") != "message":
                        continue
                    try:
                        self._dispatch(json.loads(msg.get("data") or "{}"))
                    except Exception as e:
                        logger.warning(f"AdminBridge 命令处理异常: {e}")
            except Exception as e:
                logger.warning(f"AdminBridge 订阅断开，5s 重连: {e}")
                time.sleep(5)

    def _dispatch(self, cmd: dict) -> None:
        action = cmd.get("action", "")
        account_id = str(cmd.get("account_id", "a1"))
        cid = str(cmd.get("cid", ""))

        # V18.2: 账号管理命令（account_start/stop/restart/reload_accounts）
        account_mgmt = {"account_start", "account_stop", "account_restart",
                        "account_reload", "reload_accounts"}
        if action in account_mgmt:
            cb = getattr(self, "_on_account_mgmt", None)
            if cb:
                real_action = action.replace("account_", "") or "reload"
                cb(real_action, account_id)
            else:
                logger.warning("收到账号管理命令但未挂接处理器")
            return

        if not cid:
            return
        if action == "send":
            toid = str(cmd.get("toid") or self._lookup_toid(cid))
            text = str(cmd.get("text", "")).strip()
            if not toid or not text:
                self.publish_event("send_result", {"cid": cid, "ok": False,
                                                   "error": "缺 toid/text"})
                return
            # 独立线程执行（_send_with_retry 带无限重试，不能卡订阅循环）
            threading.Thread(
                target=self._do_send, args=(account_id, cid, toid, text), daemon=True
            ).start()
        elif action == "takeover":
            self._on_takeover(account_id, cid)
            self.publish_event("takeover", {"cid": cid, "account_id": account_id})
            logger.info(f"🔴 人工接管（后台触发）account={account_id} cid={cid}")
        elif action == "release":
            self._on_release(account_id, cid)
            self.publish_event("release", {"cid": cid, "account_id": account_id})
            logger.info(f"🟢 恢复 AI（后台触发）account={account_id} cid={cid}")
        else:
            logger.debug(f"AdminBridge 未知命令: {action}")

    def _lookup_toid(self, cid: str) -> str:
        with self._lock:
            return str(self.toid_map.get(cid, {}).get("toid", "") or "")

    def _do_send(self, account_id: str, cid: str, toid: str, text: str) -> None:
        try:
            result = self._on_send(account_id, cid, toid, text)
            ok = bool(result.get("ok") or result.get("message_id"))
            self.publish_event("send_result", {
                "cid": cid, "ok": ok, "text": text[:200],
                "account_id": account_id, "error": result.get("error"),
            })
            logger.info(f"AdminBridge 人工消息发送 account={account_id} cid={cid} ok={ok}")
        except Exception as e:
            self.publish_event("send_result", {"cid": cid, "ok": False,
                                               "error": str(e)[:200]})
            logger.warning(f"AdminBridge 人工消息发送异常: {e}")


__all__ = ["AdminBridge", "COMMAND_CHANNEL", "EVENTS_KEY"]
