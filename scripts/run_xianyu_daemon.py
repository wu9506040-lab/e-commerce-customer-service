"""
闲鱼自动化 daemon 主进程（V16.1.7）

启动：
  - XianyuWatcher（subprocess 跑 goofish message watch）
  - DebounceQueue（30s 静默合并）
  - ReplyScheduler（随机延迟 + /chat API + send_message）
  - XianyuHealth（健康检查 + 失败告警）

部署：
  - Windows: NSSM 注册为服务（scripts/xianyu/install_service.bat）
  - Linux: systemd unit（scripts/xianyu/README.md 模板）

用法：
  python scripts/run_xianyu_daemon.py           # 前台跑（看 log）
  python scripts/run_xianyu_daemon.py --once   # 跑一次后退出（debug）
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
    "mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4",
)
os.environ.setdefault("APP_ENV", "prod")  # daemon 默认 prod

# 配置：必须显式开
os.environ["ENABLE_XIANYU_TOOLS"] = "true"

from app.core.config import settings  # noqa: E402
from app.integrations import get_xianyu_client  # noqa: E402
from app.services.xianyu.debounce_queue import DebounceQueue  # noqa: E402
from app.services.xianyu.health import XianyuHealth  # noqa: E402
from app.services.xianyu.reply_scheduler import ReplyScheduler  # noqa: E402
from app.services.xianyu.watcher import XianyuWatcher  # noqa: E402


# =============================================================
# 日志
# =============================================================
LOG_DIR = Path(os.environ.get("XIANYU_LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "xianyu_daemon.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("xianyu_daemon")


# =============================================================
# 持久化
# =============================================================
STATE_FILE = Path(os.environ.get("XIANYU_STATE_FILE", "logs/xianyu_state.json"))


def load_state(health: XianyuHealth) -> None:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            health.from_dict(data)
            logger.info(
                f"恢复状态 errors_count={health.errors_count} "
                f"last_error={health.last_error!r}"
            )
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


# =============================================================
# 主流程
# =============================================================
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true", help="跑一次后退出（debug 用）"
    )
    args = parser.parse_args()

    # V16.5: 接入全链路 runtime_log（daemon 是独立进程,uvicorn 的 logger 用不上,
    # 必须 daemon 自己初始化一份,才能把真实闲鱼买家对话写到 logs/xianyu_runtime.log）
    try:
        from app.core.runtime_log import setup_runtime_logging
        setup_runtime_logging(level=getattr(settings, "LOG_LEVEL", "INFO"), log_dir="logs")
    except Exception as _e:
        logger.warning(f"daemon 接入 runtime_log 失败: {_e}")

    if not settings.ENABLE_XIANYU_TOOLS:
        logger.error(
            "ENABLE_XIANYU_TOOLS=False，daemon 不会启动。\n"
            "请在 .env.dev / .env.prod 加 ENABLE_XIANYU_TOOLS=true"
        )
        return 1

    client = get_xianyu_client()
    health = XianyuHealth(client=client, daily_quota=50, alert_threshold=3)
    load_state(health)
    scheduler = ReplyScheduler(
        xianyu_client=client,
        chat_api_url=os.environ.get(
            "XIANYU_CHAT_API_URL", "http://localhost:8000/api/chat"
        ),
        min_delay_seconds=float(os.environ.get("XIANYU_MIN_DELAY", "30")),
        max_delay_seconds=float(os.environ.get("XIANYU_MAX_DELAY", "180")),
        rate_limit_per_minute=int(os.environ.get("XIANYU_RATE_LIMIT_PER_MIN", "1")),
    )
    debounce = DebounceQueue(
        fire_callback=scheduler.on_fire,
        window_seconds=float(os.environ.get("XIANYU_DEBOUNCE_WINDOW", "30")),
    )
    watcher = XianyuWatcher(callback=debounce.on_message)

    # 信号处理：优雅关闭
    stop_event = {"stop": False}

    def _on_signal(signum, frame):
        logger.info(f"收到信号 {signum}，开始关闭")
        stop_event["stop"] = True

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    # 启动
    try:
        watcher.start()
        logger.info(
            f"XianyuDaemon 启动: chat_api={os.environ.get('XIANYU_CHAT_API_URL', 'http://localhost:8000/api/chat')} "
            f"delay={os.environ.get('XIANYU_MIN_DELAY', '30')}-{os.environ.get('XIANYU_MAX_DELAY', '180')}s "
            f"debounce={os.environ.get('XIANYU_DEBOUNCE_WINDOW', '30')}s"
        )
    except Exception as e:
        logger.exception(f"watcher 启动失败: {e}")
        return 1

    # 主循环
    try:
        if args.once:
            time.sleep(5)  # 给 watcher 时间连上
        else:
            while not stop_event["stop"]:
                time.sleep(10)
                # 定时健康检查 + 持久化
                status = health.check_all()
                if status["should_alert"]:
                    logger.error(
                        f"XianyuHealth ALERT: errors={status['errors_count']} "
                        f"last_error={status['last_error']!r} "
                        f"issues={status.get('issues')}"
                    )
                save_state(health)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt，开始关闭")
    finally:
        logger.info("关闭中...")
        debounce.shutdown()
        watcher.stop()
        save_state(health)
        logger.info("XianyuDaemon 已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())