"""
闲鱼自动化 daemon 主进程（V18.2 多账号版）

架构（V18.2）：
  AccountManager（读取 config/accounts.json）
    └─ AccountWorker × N（每账号独立 ws/scheduler/debounce/recorder/工单）
         └─ AdminBridge 共享（命令/事件带 account_id）
启动：
  python scripts/run_xianyu_daemon.py    # 前台跑（watchdog 守护）
"""
import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

# 注入路径（scripts/ 与 backend/ 平级）
BACKEND_DIR = (
    Path(__file__).resolve().parent.parent / "backend"
)
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

# 脱机占位环境变量（与 demo 一致）
os.environ.setdefault(
    "JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
)
os.environ.setdefault(
    "DATABASE_URL",
    # daemon 是宿主机进程：用 host-mapped port 3307（compose 暴露 3306→3307）
    "mysql+pymysql://cs_user:dev_user_2026@127.0.0.1:3307/customer_service?charset=utf8mb4",
)
os.environ.setdefault("APP_ENV", "dev")

os.environ["ENABLE_XIANYU_TOOLS"] = "true"


def _load_dotenv():
    """简陋 .env 解析：读 deploy/.env.dev 加载 KEY=VALUE 到 os.environ"""
    env_path = Path(__file__).resolve().parent.parent / "deploy" / ".env.dev"
    if not env_path.exists():
        print(f"[daemon] .env.dev 不存在: {env_path}", file=sys.stderr)
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[daemon] .env.dev 读失败: {e}", file=sys.stderr)
        return
    loaded = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        if k not in os.environ or not os.environ[k]:
            os.environ[k] = v
            loaded += 1
    print(f"[daemon] 从 .env.dev 加载 {loaded} 个环境变量", file=sys.stderr)


_load_dotenv()

# 端到端延迟优化默认（V18-P0）
os.environ.setdefault("XIANYU_DEBOUNCE_WINDOW", "4")
os.environ.setdefault("XIANYU_FIRST_MIN_DELAY", "0")
os.environ.setdefault("XIANYU_FIRST_MAX_DELAY", "3")
os.environ.setdefault("XIANYU_NEXT_MIN_DELAY", "0")
os.environ.setdefault("XIANYU_NEXT_MAX_DELAY", "1")

from app.core.config import settings  # noqa: E402

# =============================================================
# 日志
# =============================================================
_REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = _REPO_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "xianyu_daemon.log"
PID_FILE = LOG_DIR / "xianyu_daemon.pid"

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.ERROR)
stderr_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
)
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, stderr_handler],
)
logger = logging.getLogger("xianyu_daemon")

STATE_FILE = Path(os.environ.get("XIANYU_STATE_FILE", "logs/xianyu_state.json"))

from app.services.xianyu.health import XianyuHealth  # noqa: E402


def load_state(health: XianyuHealth) -> None:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            health.from_dict(data)
            logger.info(f"恢复状态 errors_count={health.errors_count}")
        except Exception as e:
            logger.warning(f"load_state 失败: {e}")


def save_state(health: XianyuHealth) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(health.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"save_state 失败: {e}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="跑一次后退出（debug）")
    args = parser.parse_args()

    try:
        from app.core.runtime_log import setup_runtime_logging
        setup_runtime_logging(level=getattr(settings, "LOG_LEVEL", "INFO"), log_dir="logs")
    except Exception as _e:
        logger.warning(f"daemon 接入 runtime_log 失败: {_e}")

    # =============================================================
    # V18.2 多账号：AccountManager（注册表 config/accounts.json）
    # =============================================================
    from app.clients.mysql_client import get_session_local
    from app.services.xianyu.account_manager import AccountManager
    from app.services.xianyu.conversation_recorder import ConversationRecorder
    from app.services.xianyu.notification_service import load_config as load_notify

    import redis as redis_lib

    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    _r = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    recorder = ConversationRecorder(get_session_local())
    chat_api_url = os.environ.get(
        "XIANYU_CHAT_API_URL", "http://localhost:8000/api/chat"
    )

    # 单账号兼容：accounts.json 缺失时 AccountManager 回退默认 a1（cookie.txt）
    manager = AccountManager(
        redis_client=_r, recorder=recorder, bridge=None, chat_api_url=chat_api_url,
    )
    accounts = manager.load_accounts()
    logger.info(f"账号注册表: {[(a.get('id'), a.get('name'), a.get('enabled')) for a in accounts]}")

    # AdminBridge（共享，命令带 account_id 分发到对应 worker）
    from app.services.xianyu.admin_bridge import AdminBridge

    bridge = AdminBridge(
        redis_url=redis_url,
        on_send=lambda account_id, cid, toid, text: manager.route_send(
            account_id, cid, toid, text
        ),
        on_takeover=lambda account_id, cid: manager.route_takeover(account_id, cid),
        on_release=lambda account_id, cid: manager.route_release(account_id, cid),
    )
    manager.bridge = bridge
    manager.start_all()  # 构建并启动全部 enabled 账号（含 bridge 回调挂接）

    # 全局健康（沿用：cookie 存在性 + 错误计数；worker 级状态在 xianyu:workers）
    class _LocalCookieHealth:
        def __init__(self):
            pass

        def is_enabled(self) -> bool:
            try:
                cfg = load_notify() if False else None
            except Exception:
                pass
            return True  # 多账号模式：worker 级健康见 xianyu:workers

    health = XianyuHealth(client=_LocalCookieHealth(), daily_quota=50, alert_threshold=3)
    load_state(health)

    bridge.start()
    logger.info("V18.2 多账号 daemon 就绪（xianyu:commands 订阅 + xianyu:workers 状态上报）")

    # PID file（watchdog 存活判定）
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        logger.info(f"PID file 写入: {PID_FILE} = {os.getpid()}")
    except Exception as e:
        logger.warning(f"PID file 写入失败: {e}")

    stop_event = {"stop": False}

    def _on_signal(signum, frame):
        logger.info(f"收到信号 {signum}，开始关闭")
        stop_event["stop"] = True

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    # 主循环：健康检查 + 状态持久化（worker 重连自愈在 worker 线程内）
    try:
        if args.once:
            time.sleep(5)
        else:
            consecutive_errors = 0
            while not stop_event["stop"]:
                time.sleep(10)
                try:
                    status = health.check_all()
                    if status["should_alert"]:
                        logger.error(
                            f"XianyuHealth ALERT: errors={status['errors_count']} "
                            f"last_error={status['last_error']!r}"
                        )
                    save_state(health)
                    consecutive_errors = 0
                except Exception as loop_err:
                    consecutive_errors += 1
                    logger.exception(
                        f"主循环异常 #consecutive={consecutive_errors}: "
                        f"{type(loop_err).__name__}: {loop_err}"
                    )
                    if consecutive_errors >= 10:
                        logger.error("主循环连续 10 次异常，主动退出让 watchdog 重启")
                        stop_event["stop"] = True
                        break
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt，开始关闭")
    finally:
        logger.info("关闭中...")
        manager.stop_all()
        save_state(health)
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"PID file 删除失败: {e}")
        logger.info("XianyuDaemon 已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
