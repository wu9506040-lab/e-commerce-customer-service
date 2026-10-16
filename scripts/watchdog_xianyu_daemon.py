"""
XianyuDaemon Watchdog — V16.8 P1-E2 外部看门狗

设计原则：
- V16.8 P1 容器化因 cookie 跨平台兼容性暂未通 — host daemon 仍是最稳方案
- V16.8 P1-E1 daemon main loop 已 try/except + logger.exception 兜底（防止 silent exit）
- V16.8 P1-E2 (此脚本)：**外部看门狗**作为最后一层保险
  - 30s 探测一次 daemon 进程
  - 缺则立刻 restart（后台 spawn run_xianyu_daemon.py）
  - 适合 host daemon 跑（Windows 任务计划 / startup 启动 watchdog → 拉 daemon）

调用方式：
  python scripts/watchdog_xianyu_daemon.py
  或后台启动（DETACHED_PROCESS + 关闭 stdin）：

日志：
- logs/xianyu_daemon_watchdog.log：所有生命周期事件（INFO/WARNING/ERROR）
- stderr：ERROR+ 级别（与 daemon 一致 — 宿主机 vs 容器都可见）
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# =============================================================
# 路径常量
# =============================================================
_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent / "backend"
_DAEMON_SCRIPT = _SCRIPT_DIR / "run_xianyu_daemon.py"
_REPO_ROOT = _SCRIPT_DIR.parent
_LOG_FILE = _REPO_ROOT / "logs" / "xianyu_daemon_watchdog.log"
_DAEMON_LOG_DIR = _REPO_ROOT / "logs"
_DAEMON_LOG_FILE = _DAEMON_LOG_DIR / "xianyu_daemon_watchdog_spawn.log"

# 检测间隔（秒）
CHECK_INTERVAL_SEC = 30

# =============================================================
# 日志：双 handler — file (INFO+) + stderr (ERROR+)
# =============================================================
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | watchdog | %(message)s")
)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.ERROR)
stderr_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | watchdog | %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, stderr_handler])
logger = logging.getLogger("watchdog")


# =============================================================
# 检测 + 重启
# =============================================================
def _query_daemon_via_powershell() -> list[int]:
    """用 PowerShell Get-Process 查含 run_xianyu_daemon.py 的 python 进程

    Returns:
        list of pid，daemon 命中数（通常 1）

    V16.8 P1-E2: 旧版用 wmic — Windows 11 默认移除 wmic（FileNotFoundError）
    现改 PowerShell（系统自带），CommandLine filter 精准匹配 daemon 脚本
    """
    ps_script = (
        "Get-Process python -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -like '*run_xianyu_daemon.py*' } | "
        "Select-Object -ExpandProperty Id"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"PowerShell 查询失败: {type(e).__name__}: {e}")
        return []

    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def is_daemon_alive() -> bool:
    """返回 True 表示至少 1 个 daemon 进程在跑"""
    return len(_query_daemon_via_powershell()) >= 1


def spawn_daemon() -> int | None:
    """后台启动 daemon，返 PID

    - 用 sys.executable (Python 解释器)
    - **强制**设 APP_ENV=dev（避免 host shell APP_ENV=prod 覆盖）
    - stdout/stderr 重定向到 logs/xianyu_daemon_watchdog_spawn.log
    - Windows 下用 DETACHED_PROCESS 让 daemon 独立存在
    - 写 PID file（logs/xianyu_daemon.pid）供 watchdog 检测 PID alive
    - 用 PowerShell 重定向而非 DETACHED_PROCESS 单独 file handle
      （避免 detach 后 fd inheritance 问题，0 字节 spawn log 不再出现）
    """
    env = os.environ.copy()
    # V16.8 P1-E2 修正：必须强制覆盖（不只 setdefault，因为 shell 可能已经有 prod）
    env["APP_ENV"] = "dev"
    # V16.8 P1 默认延迟参数
    env["XIANYU_DEBOUNCE_WINDOW"] = "8"
    env["XIANYU_FIRST_MIN_DELAY"] = "0"
    env["XIANYU_FIRST_MAX_DELAY"] = "3"

    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP

    # 打开 log file 用 line buffering（Python 端 flush 即可）
    log_fh = open(_DAEMON_LOG_FILE, "a", encoding="utf-8")

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(_DAEMON_SCRIPT)],
            cwd=str(_BACKEND_DIR),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,  # 合并到 stdout file
            stdin=subprocess.DEVNULL,
            creationflags=flags if flags else 0,
            close_fds=True,
        )
        return proc.pid
    finally:
        log_fh.close()  # 子进程已继承 fd，关闭父进程 fd 安全


# =============================================================
# 主循环
# =============================================================
def main() -> int:
    logger.info(
        f"watchdog 启动: 每 {CHECK_INTERVAL_SEC}s 检查 daemon 状态，"
        f"daemon 脚本={_DAEMON_SCRIPT.name}"
    )
    # 启动时 sanity check：daemon 脚本存在吗
    if not _DAEMON_SCRIPT.exists():
        logger.error(f"daemon 脚本不存在: {_DAEMON_SCRIPT}")
        return 1

    consecutive_missing = 0
    while True:
        try:
            alive = is_daemon_alive()
            if alive:
                if consecutive_missing > 0:
                    logger.info(
                        f"daemon 恢复（之前缺失 {consecutive_missing} 次）"
                    )
                consecutive_missing = 0
            else:
                consecutive_missing += 1
                logger.error(
                    f"daemon 缺失 #{consecutive_missing}"
                )
                # 1 次缺失就重启（避免误判延后恢复）
                logger.warning("尝试重启 daemon...")
                try:
                    new_pid = spawn_daemon()
                    if new_pid:
                        logger.info(
                            f"daemon 重启命令已发 pid={new_pid}"
                        )
                    else:
                        logger.error("spawn_daemon 返 None")
                except Exception as spawn_err:
                    logger.exception(
                        f"重启 daemon 异常: {type(spawn_err).__name__}: {spawn_err}"
                    )
        except Exception as loop_err:
            # watchdog 自身异常不退出（P1-E 同义：永不 silent exit）
            logger.exception(
                f"watchdog 主循环异常: {type(loop_err).__name__}: {loop_err}"
            )

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main() or 0)
