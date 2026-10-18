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
  或后台启动（V16.8 P1-E2 选项 D：不 detach + PID file 协议）：

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
# V16.8 P1-E2 选项 D：daemon 写 PID file，watchdog 仅看这个文件存在性
_PID_FILE = _REPO_ROOT / "logs" / "xianyu_daemon.pid"

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
def _pid_alive(pid: int) -> bool:
    """Windows 安全探活 —— ⚠️ 绝不能用 os.kill(pid, 0)！

    Windows 上 os.kill 不支持 signal 0 探活：它会走 TerminateProcess 把目标
    进程直接杀掉（V18 实测：watchdog 一启动就把 daemon 杀了，还抛 SystemError）。
    正确姿势：OpenProcess + GetExitCodeProcess == STILL_ACTIVE。
    """
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False  # 进程不存在 / 无权限
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_daemon_alive() -> bool:
    """V16.8 P1-E2 选项 D：PID file 存在 + PID 进程真活着（V18 增强）

    V18 修正：daemon 硬崩溃时 finally 不执行，PID file 会残留 —— 只看文件
    会让看门狗失明。V18 又踩一坑：os.kill(pid, 0) 在 Windows 上是 TerminateProcess
    （直接杀进程），必须用 OpenProcess 探活。
    """
    if not _PID_FILE.exists():
        return False
    pid = _read_pid_file()
    if pid is None or pid <= 0:
        return False
    return _pid_alive(pid)


def _read_pid_file() -> int | None:
    """读 PID file 内容（int），失败返 None"""
    try:
        text = _PID_FILE.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def _load_dotenv_into_env(env: dict[str, str]) -> None:
    """简陋的 .env.dev 解析（不引入依赖）

    V16.8 P1-E2: daemon config 验证需要 JWT_SECRET/DATABASE_URL/QWEN_API_KEY 等
    host shell 通常无这些变量，watchdog 必须 source .env.dev 才能 spawn 成功 daemon
    """
    env_path = _REPO_ROOT / "deploy" / ".env.dev"
    if not env_path.exists():
        logger.warning(f".env.dev 不存在: {env_path}")
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f".env.dev 读失败: {e}")
        return
    loaded = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        # 去引号
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        # 不覆盖强制的关键变量
        if k not in env or not env[k]:
            env[k] = v
            loaded += 1
    logger.info(f"从 {env_path.name} 加载 {loaded} 个环境变量（不覆盖已设值）")


def spawn_daemon() -> int | None:
    """V16.8 P1-E2 选项 D：subprocess.Popen 启动 daemon（不用 DETACHED_PROCESS）

    - **不**用 DETACHED_PROCESS（V16.8 P1 旧版 spawn log 0 字节 + 空 CommandLine）
    - stdout/stderr 重定向到 spawn log file
    - daemon 启动后自己写 PID file；watchdog 用 PID file 检测存活（独立于进程 API）
    - 子进程与 watchdog 同 process tree（watchdog exit → daemon 也 exit）
    """
    env = os.environ.copy()
    # V16.8 P1-E2 修正：必须强制覆盖（不只 setdefault，因为 shell 可能已经有 prod）
    env["APP_ENV"] = "dev"
    # 加载 .env.dev（关键：JWT_SECRET / QWEN_API_KEY / DATABASE_URL 等不在 host shell）
    _load_dotenv_into_env(env)
    # V16.8 P1 默认延迟参数（覆盖 .env 里的可能值）
    env["XIANYU_DEBOUNCE_WINDOW"] = "4"
    env["XIANYU_FIRST_MIN_DELAY"] = "0"
    env["XIANYU_FIRST_MAX_DELAY"] = "3"
    # V16.8 P3: 用户 F12 抓的 cookie 字符串 (raw 'k=v; k=v'), 让 daemon 用自实现 WS 而非 goofish-cli
    env["XIANYU_COOKIE_HEADER"] = os.environ.get("XIANYU_COOKIE_HEADER", "")
    # V16.8 P2-A 决策: 当前在 Windows 上 WSS 卡死 + HTTP 轮询不实时
    # 回退到 XianyuWatcher (WebSocket 长连接) 模式
    # 部署 Linux 后再考虑切换回 poller 或 WS 短连接方案
    env["XIANYU_USE_POLLER"] = "false"

    # V16.8 P1-E2 选项 D：不带 creationflags（不 DETACHED_PROCESS，让 daemon 与 watchdog 同 tree）
    log_fh = open(_DAEMON_LOG_FILE, "a", encoding="utf-8")

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(_DAEMON_SCRIPT)],
            cwd=str(_BACKEND_DIR),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # 不带 creationflags — 不 detach，简化 fd 继承
        )
        logger.info(
            f"spawn daemon pid={proc.pid} APP_ENV=dev (jwt_secret={'set' if env.get('JWT_SECRET') else 'MISSING'})"
        )
        return proc.pid
    finally:
        log_fh.close()


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
