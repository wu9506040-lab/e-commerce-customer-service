"""
XianyuWatcher — 闲鱼 IM 守护进程（V16.1）

职责：
1. subprocess 跑 `goofish message watch --format json`（长连接 stdin 实时推 JSONL）
2. 读 stdout 按行 parse JSON
3. 调 callback（如 DebounceQueue）
4. watchdog：subprocess 死 → 自动重启（最多 5 次）
5. 探活：启动时跑 `goofish auth status` 确认 cookie 有效
6. 优雅关闭：stop() 关闭 subprocess + reader 线程

调用方：
  ReplyScheduler（或 main 入口）→ XianyuWatcher(callback=on_message)
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from app.integrations.xianyu_config import GOOFISH_MCP_CMD
from app.integrations.xianyu_mcp_client import XianyuMCPClient

logger = logging.getLogger(__name__)

# 最大重启次数（5 次后永久停，避免雪崩）
MAX_RESTART_COUNT = 5
# watchdog 心跳间隔（秒）
WATCHDOG_INTERVAL = 5.0
# V16.5: Stale event watchdog — 检测 goofish 长连接"假死"（subprocess 进程在但 stdin 静默）
# 23:15 场景:daemon 显示 WATCHER alive + 23:15:20 new_msg + 23:15:23 read 后 stdin 静默 4 分钟
# 实际长连接已断,傻瓜 watch 子进程不死,daemon 收不到消息
# 现在: 60s 无 message 事件 → ERROR 日志 + kill subprocess + 重启
STALE_EVENT_THRESHOLD_SEC = 60.0
STALE_WATCHDOG_INTERVAL_SEC = 30.0
# V16.6: Stale watchdog 指数退避（30s/60s/120s/240s/480s）
# 旧逻辑：连续重启超 5 次就 stop_event.set() 永久停
# 实际：限流场景下用户沉默，重启没意义；超次应继续监控不永久停
STALE_BACKOFF_BASE_SEC = 30.0
STALE_BACKOFF_MAX_SEC = 480.0


class WatcherAuthError(RuntimeError):
    """启动时 cookie/auth 探活失败"""


def run_auth_status() -> dict:
    """调 goofish auth status → 返 dict（valid 字段）"""
    import contextlib

    cmd = GOOFISH_MCP_CMD
    # 用 XianyuMCPClient.health_check 复用之前的 auth status 解析
    # 不直接用 client.health_check（会要求 cookie 存在），用更底层的子进程
    try:
        # 临时跑 goofish auth status —— 用 module-level GOOFISH_MCP_CMD 的母命令
        # 因为 GOOFISH_MCP_CMD 是 goofish-mcp（MCP server），不是 goofish（CLI）
        # 实际 auth status 走 goofish.exe（CLI）
        # 这里用 XianyuMCPClient 内的 auth status 等价实现
        client = XianyuMCPClient()
        # 简化：health_check 内部不调 auth status，只查本地状态
        # 真 auth status 需要调 goofish CLI
        # 用 subprocess 直接调
        goofish_cli = cmd.replace("goofish-mcp.exe", "goofish.exe")
        result = subprocess.run(
            [goofish_cli, "auth", "status", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {"unb": "", "tracknick": "", "valid": False}
        data = json.loads(result.stdout)
        return data
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        Exception,
    ) as e:
        logger.warning(f"run_auth_status failed: {type(e).__name__}: {e}")
        return {"unb": "", "tracknick": "", "valid": False}


class XianyuWatcher:
    """闲鱼 IM 守护进程

    设计取舍（V16.1）：
    - 单进程单 subprocess（不并发；并发会触发闲鱼风控 RGV587）
    - watchdog 心跳 5s 间隔（实时性 OK）
    - callback 同步调用（业务方负责 thread-safety）
    - JSONL 按行 parse（不会跨行 split —— goofish 已保证每行一条事件）
    """

    def __init__(
        self,
        callback: Callable[[dict], None],
        max_restart: int = MAX_RESTART_COUNT,
        watchdog_interval: float = WATCHDOG_INTERVAL,
    ):
        self._callback = callback
        self._max_restart = max_restart
        self._watchdog_interval = watchdog_interval
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        # V16.5: Stale event 检测 — 上次收到 message 事件的时间
        self._last_message_event_at: float = 0.0
        self._stale_watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._restart_count = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """启动 watcher（幂等：重复 start 是 no-op）

        流程：
        1. 探活（goofish auth status）
        2. 起 subprocess（goofish message watch --format json）
        3. 起 reader 线程读 stdout
        4. 起 watchdog 线程

        Raises:
            WatcherAuthError: cookie 失效 / 未登录
            RuntimeError: 已在运行
        """
        with self._lock:
            if self.is_alive():
                logger.info("XianyuWatcher: 已在运行，start 跳过")
                return
            # 1. 探活
            status = run_auth_status()
            if not status.get("valid"):
                raise WatcherAuthError(
                    f"cookie/auth 失效：{status.get('unb') or '未登录'} — "
                    f"请先 goofish auth login 重新导入"
                )
            # 2. 起 subprocess
            self._spawn_subprocess()
            # 3. reader 线程
            self._stop_event.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="xianyu-watcher-reader",
                daemon=True,
            )
            self._reader_thread.start()
            # 4. watchdog 线程
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="xianyu-watcher-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()
            # V16.5: Stale event watchdog(独立线程,检测 stdin 静默)
            self._start_stale_watchdog()
            logger.info(
                f"XianyuWatcher started: unb={status.get('unb')} "
                f"tracknick={status.get('tracknick')}"
            )

    def stop(self) -> None:
        """优雅关闭：subprocess + 线程"""
        with self._lock:
            self._stop_event.set()
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("XianyuWatcher subprocess terminate 超时，强杀")
                    self._proc.kill()
            self._proc = None
            # 线程是 daemon=True，主进程退出时自动结束；不强制 join
            self._reader_thread = None
            self._watchdog_thread = None
            logger.info("XianyuWatcher stopped")

    def is_alive(self) -> bool:
        """watcher 是否在运行（subprocess alive + 未 stop）"""
        return (
            self._proc is not None
            and self._proc.poll() is None
            and not self._stop_event.is_set()
        )

    # =============================================================
    # 内部
    # =============================================================
    def _spawn_subprocess(self) -> None:
        """起 goofish message watch 子进程（stdout PIPE）"""
        cmd = GOOFISH_MCP_CMD
        # GOOFISH_MCP_CMD 实际是 goofish-mcp.exe（MCP server）
        # 但 watch 是 goofish CLI 子命令（不是 MCP tool）
        # 替换 .exe 名（用 CLI 版的 goofish.exe）
        goofish_cli = cmd.replace("goofish-mcp.exe", "goofish.exe")
        self._proc = subprocess.Popen(
            [goofish_cli, "message", "watch", "--format", "json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # 行缓冲
            encoding="utf-8",  # V16.3.1 修：避免 gbk 解码崩溃
            errors="replace",  # 不可解码字节用 ? 代替
        )

    def _reader_loop(self) -> None:
        """读 stdout，按行 parse JSON，调 callback"""
        if not self._proc or not self._proc.stdout:
            return
        try:
            for line in self._proc.stdout:
                if self._stop_event.is_set():
                    break
                line = line.rstrip("\n").rstrip("\r")
                if line:
                    self._handle_line(line)
        except Exception as e:
            logger.exception(
                f"XianyuWatcher reader 异常: {type(e).__name__}: {e}"
            )

    def _handle_line(self, line: str) -> None:
        """单行 JSONL 解析

        设计：
        - 空行 / 纯空白 → 跳过
        - 坏 JSON → 跳过（不抛；不影响主流程）
        - 非 message 事件（connected/heartbeat）→ 跳过
        - message 事件 → 调 callback
        """
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(f"XianyuWatcher 跳过坏 JSON: {line[:80]!r}")
            return
        # DEBUG: 看真实事件格式
        logger.info(f"XianyuWatcher DEBUG event: {line[:300]}")
        if not isinstance(event, dict):
            return
        event_type = event.get("event", "")
        # V16.5: Stale watchdog 心跳 — 任何 message 事件都更新"最后收到"时间戳
        if event_type == "message":
            self._last_message_event_at = time.time()
        if event_type != "message":
            # 兼容：有些事件可能没 event 字段但有 message 字段
            if "message" not in event and "cid" not in event:
                return
        try:
            self._callback(event)
        except Exception as e:
            # callback 异常不阻断 reader（记 log 即可）
            logger.exception(
                f"XianyuWatcher callback 异常: {type(e).__name__}: {e}"
            )

    def _start_stale_watchdog(self) -> None:
        """V16.5: 启动 stale event 检测线程（独立的后台 daemon thread）

        检测场景：subprocess 进程在但 stdin 静默（WebSocket 长连接死了，
        但 goofish watch 子进程没退出 → 现有 _watchdog_tick 不杀）
        """
        if self._stale_watchdog_thread is not None:
            return  # 幂等
        self._stale_watchdog_thread = threading.Thread(
            target=self._stale_watchdog_loop,
            name="xianyu-watcher-stale-watchdog",
            daemon=True,
        )
        self._stale_watchdog_thread.start()
        logger.info(
            f"XianyuWatcher stale watchdog 已启动: "
            f"threshold={STALE_EVENT_THRESHOLD_SEC}s "
            f"check_interval={STALE_WATCHDOG_INTERVAL_SEC}s"
        )

    def _stale_watchdog_loop(self) -> None:
        """每 30s 检查一次: 最后一次收到 message 事件距现在是否 > 60s

        → 是: kill subprocess + 调 _spawn_subprocess() 重启
        """
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=STALE_WATCHDOG_INTERVAL_SEC):
                break  # stop
            self._stale_watchdog_tick()
            # 也开 LOG 标记 watchdog 还在跑(简单健康检查)
            # 不发到 runtime_log(避免噪音), 仅 stderr
            # V16.5 fix: 'never':.1f 是非法 f-string 格式('never' 是 str,不能 .1f)
            # 改为 'never' 文本或 '0.0' float;这里用条件分支输出字符串
            if self._last_message_event_at:
                age_str = f"{(time.time() - self._last_message_event_at):.1f}s"
            else:
                age_str = "never"
            logger.debug(
                f"XianyuWatcher stale watchdog tick: last_msg_age={age_str}"
            )

    def _stale_watchdog_tick(self) -> None:
        """单次 stale 检查 + 必要时重启 subprocess（V16.6 指数退避）

        V16.6 fix: 旧逻辑超 5 次重启就 stop_event.set() 永久停
        → 实际场景（限流后用户沉默）会导致 daemon 永久死掉，只能手动重启
        现在：
        - 1-5 次重启：指数 backoff 30s/60s/120s/240s/480s（用 _stop_event.wait 可被 stop 打断）
        - 6+ 次：backoff 0（节省时间），但不再永久停，仅 ERROR 日志持续打
        """
        if self._stop_event.is_set() or self._proc is None:
            return
        # 还没收到过任何 message → 不判 stale（避免启动时误判）
        if self._last_message_event_at <= 0:
            return
        # 检测 subprocess 是否还活(可能在死状态)
        if self._proc.poll() is not None:
            # subprocess 已死,交由 _watchdog_tick 处理
            return
        idle_sec = time.time() - self._last_message_event_at
        if idle_sec <= STALE_EVENT_THRESHOLD_SEC:
            return
        # Stale detected — 重启
        self._restart_count += 1

        # 1-5 次：指数 backoff（30s/60s/120s/240s/480s）
        # 6+ 次：backoff 0（节省时间）
        if self._restart_count <= self._max_restart:
            backoff_sec = min(
                STALE_BACKOFF_BASE_SEC * (2 ** (self._restart_count - 1)),
                STALE_BACKOFF_MAX_SEC,
            )
        else:
            backoff_sec = 0.0

        logger.error(
            f"XianyuWatcher STALE DETECTED: idle={idle_sec:.0f}s "
            f"(>{STALE_EVENT_THRESHOLD_SEC}s),"
            f"重启 subprocess #{self._restart_count}/{self._max_restart} "
            f"backoff={backoff_sec}s"
        )
        # kill 当前 subprocess
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except Exception as e:
            logger.warning(f"XianyuWatcher kill subprocess 失败: {e}")
        # backoff sleep（用 _stop_event.wait 可被 stop 打断）
        if backoff_sec > 0:
            if self._stop_event.wait(timeout=backoff_sec):
                logger.info("XianyuWatcher stale backoff 被 stop 打断")
                return
        # 重启
        try:
            self._spawn_subprocess()
            logger.info(
                f"XianyuWatcher stale 重启完成 restart#{self._restart_count} "
                f"new_pid={self._proc.pid if self._proc else None}"
            )
        except Exception as e:
            logger.exception(
                f"XianyuWatcher stale 重启 subprocess 失败: {type(e).__name__}: {e}"
            )

        # V16.6 fix: 超 max_restart 不再永久停 — 持续监控 + ERROR 日志
        if self._restart_count > self._max_restart:
            logger.error(
                f"XianyuWatcher 已 stale 重启 {self._restart_count} 次（>{self._max_restart}），"
                f"继续监控不再永久停"
            )

    def _watchdog_loop(self) -> None:
        """watchdog：每 5s 检查 subprocess 状态，死了就重启"""
        while not self._stop_event.is_set():
            # 用 event.wait 替代 sleep（响应更快）
            if self._stop_event.wait(timeout=self._watchdog_interval):
                break
            self._watchdog_tick()

    def _watchdog_tick(self) -> None:
        """单次 watchdog 检查（测试用，方便 mock time）"""
        if self._stop_event.is_set():
            return
        if not self._proc:
            return
        if self._proc.poll() is not None:
            # subprocess 死
            self._restart_count += 1
            if self._restart_count > self._max_restart:
                logger.error(
                    f"XianyuWatcher 重启超 {self._max_restart} 次，永久停"
                )
                self._stop_event.set()
                return
            logger.warning(
                f"XianyuWatcher subprocess 死，{self._restart_count}/{self._max_restart} 次重启"
            )
            # 重新拉起（不重新探活——已探过）
            try:
                self._spawn_subprocess()
            except Exception as e:
                logger.exception(
                    f"XianyuWatcher 重启失败: {type(e).__name__}: {e}"
                )


__all__ = ["XianyuWatcher", "WatcherAuthError", "run_auth_status"]