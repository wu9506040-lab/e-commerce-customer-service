"""
AccountManager — 多账号管理器（V18.2）

职责：
  - 读取账号注册表 backend/config/accounts.json（mtime 变化自动重载注册表）
  - 为每个 enabled 账号构建并启动 AccountWorker（单账号崩溃隔离）
  - 路由 AdminBridge 命令（send/takeover/release 按 account_id 分发）
  - 工单/通知推送按账号归属
  - 周期上报 worker 状态 → Redis hash xianyu:workers（API/前端读取）

账号管理命令（bridge 下发）：
  {"action": "account_stop",    "account_id": "a2"}
  {"action": "account_start",   "account_id": "a2"}
  {"action": "account_restart", "account_id": "a2"}
  {"action": "reload_accounts"}                     # 重读 accounts.json
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ACCOUNTS_FILE = Path(__file__).resolve().parents[3] / "config" / "accounts.json"
WORKERS_KEY = "xianyu:workers"


class AccountManager:
    def __init__(self, redis_client, recorder, bridge, chat_api_url: str):
        self._r = redis_client
        self.recorder = recorder
        self.bridge = bridge          # 共享 AdminBridge（命令带 account_id 分发）
        self.chat_api_url = chat_api_url
        self.workers: dict[str, object] = {}
        self._accounts_cfg: list[dict] = []
        self._cfg_mtime = 0.0
        self._stop_flag = threading.Event()
        self.own_unbs: set[str] = set()  # 全部自家账号 unb（防双 AI 回环）

    # ---------------- 注册表 ----------------
    def load_accounts(self) -> list[dict]:
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            accounts = data.get("accounts", [])
            self._cfg_mtime = ACCOUNTS_FILE.stat().st_mtime
            self._accounts_cfg = accounts
            return accounts
        except FileNotFoundError:
            logger.warning(f"accounts.json 不存在: {ACCOUNTS_FILE}（回退单账号 a1）")
            return [{"id": "a1", "name": "默认账号", "cookie_path": "", "enabled": True}]
        except Exception as e:
            logger.warning(f"accounts.json 解析失败: {e}")
            return []

    def _config_changed(self) -> bool:
        try:
            return ACCOUNTS_FILE.stat().st_mtime != self._cfg_mtime
        except Exception:
            return False

    # ---------------- 生命周期 ----------------
    def start_all(self) -> None:
        accounts = self.load_accounts()
        if not accounts:
            logger.warning("无可用账号配置")
            return
        for acc in accounts:
            if not acc.get("enabled", True):
                logger.info(f"账号 {acc.get('id')} 已禁用，跳过")
                continue
            self._start_worker(acc)
        # 命令分发回调挂到 bridge（send/takeover/release 带 account_id）
        self.bridge.on_send = self.route_send
        self.bridge.on_takeover = self.route_takeover
        self.bridge.on_release = self.route_release
        # 账号管理命令（start/stop/restart/reload）
        self.bridge._on_account_mgmt = self.handle_mgmt
        # 管理线程：状态上报 + accounts.json 热重载
        threading.Thread(target=self._monitor_loop, name="account-monitor", daemon=True).start()

    def handle_mgmt(self, action: str, account_id: str) -> dict:
        """账号管理命令入口（bridge 下发）"""
        if action == "reload":
            result = self.reload_accounts()
            logger.info(f"账号注册表重载: {result}")
            return result
        if action == "start":
            for acc in self._accounts_cfg:
                if str(acc.get("id")) == account_id:
                    self._start_worker(acc)
                    return {"ok": True, "started": account_id}
            logger.warning(f"start: 账号 {account_id} 不在注册表")
            return {"ok": False, "error": "not found"}
        if action == "stop":
            self.stop_one(account_id)
            return {"ok": True, "stopped": account_id}
        if action == "restart":
            self.restart_one(account_id)
            return {"ok": True, "restarted": account_id}
        return {"ok": False, "error": f"未知操作 {action}"}

    def _start_worker(self, acc: dict) -> None:
        from app.services.xianyu.account_worker import AccountWorker

        aid = str(acc.get("id", "a1"))
        if aid in self.workers:
            logger.warning(f"账号 {aid} 已在运行，跳过重复启动")
            return
        w = AccountWorker(acc, recorder=self.recorder, event_sink=self.bridge.publish_event,
                          chat_api_url=self.chat_api_url)
        w._bridge_buyer_cb = lambda ev: self.bridge.record_buyer_event(ev)
        self.workers[aid] = w
        w.start()

    def stop_one(self, account_id: str) -> None:
        w = self.workers.pop(account_id, None)
        if w:
            w.stop()
            logger.info(f"账号 {account_id} 已停止")

    def restart_one(self, account_id: str) -> None:
        self.stop_one(account_id)
        for acc in self._accounts_cfg:
            if str(acc.get("id")) == account_id:
                self._start_worker(acc)
                return
        logger.warning(f"restart: 账号 {account_id} 不在注册表中")

    def stop_all(self) -> None:
        self._stop_flag.set()
        for aid in list(self.workers):
            self.stop_one(aid)

    # ---------------- bridge 命令路由 ----------------
    def route_send(self, account_id: str, cid: str, toid: str, text: str) -> dict:
        w = self.workers.get(account_id)
        if w is None or w.scheduler is None:
            return {"ok": False, "error": f"账号 {account_id} 未运行"}
        return w.scheduler._send_with_retry(cid=cid, toid=toid, text=text)

    def route_takeover(self, account_id: str, cid: str) -> None:
        w = self.workers.get(account_id)
        if w and w.scheduler and w.scheduler._manual_mode:
            w.scheduler._manual_mode.enter(cid)

    def route_release(self, account_id: str, cid: str) -> None:
        w = self.workers.get(account_id)
        if w and w.scheduler and w.scheduler._manual_mode:
            w.scheduler._manual_mode.exit(cid)

    # ---------------- 状态上报 + 热重载 ----------------
    def _monitor_loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                for aid, w in list(self.workers.items()):
                    # 收集自家 unb（worker 连上后才有）→ worker 侧防回环判定用
                    unb = getattr(w.watcher, "_unb", None) if w.watcher else None
                    if unb:
                        self.own_unbs.add(str(unb))
                        w.own_unbs_getter = lambda: self.own_unbs
                    payload = json.dumps({
                        "id": aid,
                        "name": w.name,
                        "status": w.status,
                        "last_error": w.last_error[:120],
                        "started_at": w.started_at,
                    }, ensure_ascii=False)
                    self._r.hset(WORKERS_KEY, aid, payload)
                # 已停掉的 worker 从状态里摘除
                for aid in list(self._r.hkeys(WORKERS_KEY)):
                    if aid not in self.workers:
                        self._r.hdel(WORKERS_KEY, aid)
                # accounts.json 变更 → 提示（阶段 2 管理页手动触发热重载）
                if self._config_changed():
                    logger.info("检测到 accounts.json 变更（如需生效请发 reload_accounts 命令）")
            except Exception as e:
                logger.debug(f"monitor 循环异常: {e}")
            if self._stop_flag.wait(10):
                return

    def reload_accounts(self) -> dict:
        """热重载：新账号启动、被移除/禁用的账号停止"""
        old_ids = set(self.workers)
        accounts = self.load_accounts()
        enabled_ids = set()
        started = []
        for acc in accounts:
            if not acc.get("enabled", True):
                continue
            aid = str(acc.get("id", "a1"))
            enabled_ids.add(aid)
            if aid not in self.workers:
                self._start_worker(acc)
                started.append(aid)
        for aid in old_ids - enabled_ids:
            self.stop_one(aid)
        return {"started": started, "stopped": sorted(old_ids - enabled_ids), "total": len(enabled_ids)}


__all__ = ["AccountManager", "WORKERS_KEY", "ACCOUNTS_FILE"]
