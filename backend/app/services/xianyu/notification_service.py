"""
NotificationService — 外发通知（V18.2.3 企业级中台能力）

设计参考 xianyu-auto-reply notification_manager（8 渠道 + 冷却防重），裁剪为我们需要的：

支持渠道（config/notify.json 配置驱动，热加载）：
  dingtalk  钉钉群机器人   {"type":"dingtalk","webhook":"https://oapi.dingtalk.com/robot/send?access_token=xx","secret":"SEC.."}
  wecom     企业微信群机器人 {"type":"wecom","webhook":"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xx"}
  feishu    飞书群机器人   {"type":"feishu","webhook":"https://open.feishu.cn/open-apis/bot/v2/hook/xx"}
  wxpusher  微信推送（免公众号）{"type":"wxpusher","app_token":"AT_xx","uids":["UID_xx"]}
  webhook   通用 webhook  {"type":"webhook","url":"https://..."}

事件：工单创建（转人工/退款/平台退款 P0）→ 推送所有启用通道
冷却：同事件 key 300s 防重复（P0 绕过冷却）
线程安全：推送在调用方线程内同步执行（daemon 侧由独立线程触发）
"""
from __future__ import annotations

import hashlib
import hmac
import base64
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "notify.json"
COOLDOWN_SECONDS = 300

_lock = threading.Lock()
_last_sent: dict[str, float] = {}  # event_key → ts（进程内冷却）


def load_config() -> dict:
    """读取 notify.json（每次读盘 → 改配置文件即热生效，无需重启）"""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False, "channels": [], "cooldown": COOLDOWN_SECONDS}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _post_json(url: str, payload: dict, timeout: int = 8) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")[:200]
            return 200 <= resp.status < 300, body
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _dingtalk_sign(secret: str, ts: int) -> str:
    sign = f"{ts}\n{secret}"
    h = hmac.new(secret.encode(), sign.encode(), hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(h))


def _dispatch(channel: dict, title: str, text: str) -> tuple[bool, str]:
    ctype = channel.get("type", "")
    content = f"{title}\n{text}"
    try:
        if ctype == "dingtalk":
            url = channel["webhook"]
            secret = channel.get("secret")
            if secret:
                ts = round(time.time() * 1000)
                url += f"&timestamp={ts}&sign={_dingtalk_sign(secret, ts)}"
            return _post_json(url, {"msgtype": "text", "text": {"content": content}})

        if ctype == "wecom":
            return _post_json(channel["webhook"], {"msgtype": "text", "text": {"content": content}})

        if ctype == "feishu":
            return _post_json(channel["webhook"], {"msg_type": "text", "content": {"text": content}})

        if ctype == "wxpusher":
            payload = {
                "appToken": channel["app_token"],
                "content": content,
                "summary": title,
                "contentType": 1,
            }
            if channel.get("uids"):
                payload["uids"] = channel["uids"]
            return _post_json("https://wxpusher.zjiecode.com/api/send/message", payload)

        if ctype == "webhook":
            return _post_json(channel["url"], {"title": title, "text": text})

        return False, f"未知渠道类型: {ctype}"
    except KeyError as e:
        return False, f"渠道配置缺字段: {e}"


def push_notification(title: str, text: str, event_key: str = "",
                      priority: str = "P2", force: bool = False) -> list[dict]:
    """推送到所有启用通道。

    Args:
        event_key: 冷却键（如 "ticket:68099209693"）——冷却期内同 key 不重复推
        force: True 跳过冷却（P0/测试用）
    Returns:
        每个通道的结果 [{"type":..., "ok":..., "detail":...}, ...]
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        return [{"type": "skip", "ok": False, "detail": "通知未启用（config/notify.json）"}]
    channels = [c for c in cfg.get("channels", []) if c.get("enabled")]
    if not channels:
        return [{"type": "skip", "ok": False, "detail": "无启用通道"}]

    cooldown = int(cfg.get("cooldown", COOLDOWN_SECONDS))
    if not force and event_key:
        with _lock:
            last = _last_sent.get(event_key, 0)
            if time.time() - last < cooldown:
                return [{"type": "skip", "ok": False, "detail": f"冷却中({cooldown}s)"}]
            _last_sent[event_key] = time.time()

    results = []
    for ch in channels:
        ok, detail = _dispatch(ch, title, text)
        results.append({"type": ch.get("type"), "ok": ok, "detail": detail})
        logger.info(f"通知推送[{ch.get('type')}] ok={ok} title={title[:30]}")
    return results


def push_async(title: str, text: str, event_key: str = "", priority: str = "P2") -> None:
    """线程化推送（不阻塞调用链路）"""
    threading.Thread(
        target=push_notification,
        args=(title, text, event_key, priority),
        kwargs={"force": priority == "P0"},
        daemon=True,
    ).start()


__all__ = ["load_config", "save_config", "push_notification", "push_async", "CONFIG_PATH"]
