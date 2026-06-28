"""
结构化日志配置 — M8 可观测性

提供：
- JSONFormatter：把 LogRecord 序列化为 JSON（含 request_id / session_id / user_id / intent）
- ContextFilter：从 ContextVar 注入上下文字段到每条日志
- setup_logging()：按 APP_ENV 自动切 text / json 格式

为什么用 JSON：
- 多服务串联时按 request_id 聚合日志（grep / ELK / Loki）
- 字段化便于 alert 规则（latency_ms > 3000）

为什么不直接用 structlog：
- 多一个依赖，CLAUDE.md 要求"禁止乱装依赖"
- 标准库 logging 已够用，只需补一个 Formatter + Filter
"""
import json
import logging
import sys
from datetime import datetime, timezone, timedelta

from app.core.context import (
    request_id_var,
    session_id_var,
    user_id_var,
    intent_var,
)


# =============================================================
# 时区（统一北京时间 +8）
# =============================================================
CST = timezone(timedelta(hours=8))


# =============================================================
# ContextFilter - 把 ContextVar 字段注入 LogRecord
# =============================================================
class ContextFilter(logging.Filter):
    """从 ContextVar 注入 request_id / session_id / user_id / intent 到 LogRecord

    任何 logger.info(...) 都会自动带上这四个字段（JSON / text 模式都生效）
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.session_id = session_id_var.get()
        record.user_id = user_id_var.get()
        record.intent = intent_var.get()
        return True


# =============================================================
# JSONFormatter - 输出 JSON 单行（生产 / 日志聚合用）
# =============================================================
class JSONFormatter(logging.Formatter):
    """JSON 单行日志格式

    输出示例：
        {"ts":"2026-06-28T10:23:45.123+08:00","level":"INFO","logger":"app.api.chat",
         "msg":"/chat done","request_id":"a1b2","session_id":"robust-001",
         "user_id":1,"intent":"refund_query","latency_ms":1850}
    """

    # 标准字段（直接来自 LogRecord）
    RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "asctime", "request_id", "session_id",
        "user_id", "intent", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        # 1. 基础字段
        ts = datetime.fromtimestamp(record.created, tz=CST).isoformat(timespec="milliseconds")
        log_obj = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # 2. 上下文字段（从 ContextFilter 注入）
        if getattr(record, "request_id", None) and record.request_id != "-":
            log_obj["request_id"] = record.request_id
        if getattr(record, "session_id", None) and record.session_id != "-":
            log_obj["session_id"] = record.session_id
        if getattr(record, "user_id", None):
            log_obj["user_id"] = record.user_id
        if getattr(record, "intent", None) and record.intent != "-":
            log_obj["intent"] = record.intent

        # 3. 自定义 extra 字段（业务埋点：latency_ms / hits / intent 等）
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                # 跳过非业务字段（属性 / callable）
                if callable(value):
                    continue
                try:
                    json.dumps(value)  # 试探是否可序列化
                    log_obj[key] = value
                except (TypeError, ValueError):
                    log_obj[key] = str(value)

        # 4. 异常信息
        if record.exc_info:
            log_obj["exc"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, ensure_ascii=False)


# =============================================================
# TextFormatter - 人类可读（开发用）
# =============================================================
class TextFormatter(logging.Formatter):
    """带上下文的纯文本格式

    示例：
        2026-06-28 10:23:45 [INFO] app.api.chat [req=a1b2 sid=robust-001 uid=1 intent=refund_query] /chat done (latency=1850ms)
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)s] %(name)s %(context)s%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        # 构造上下文片段
        ctx_parts = []
        if getattr(record, "request_id", None) and record.request_id != "-":
            ctx_parts.append(f"req={record.request_id[:12]}")
        if getattr(record, "session_id", None) and record.session_id != "-":
            ctx_parts.append(f"sid={record.session_id[:12]}")
        if getattr(record, "user_id", None):
            ctx_parts.append(f"uid={record.user_id}")
        if getattr(record, "intent", None) and record.intent != "-":
            ctx_parts.append(f"intent={record.intent}")
        record.context = f"[{' '.join(ctx_parts)}] " if ctx_parts else ""

        # 自定义 extra 字段（追加到 msg 末尾）
        extras = []
        RESERVED = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "asctime", "request_id", "session_id",
            "user_id", "intent", "message", "context", "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in RESERVED and not key.startswith("_") and not callable(value):
                extras.append(f"{key}={value}")
        if extras:
            record.msg = f"{record.msg} ({', '.join(extras)})"

        return super().format(record)


# =============================================================
# 主入口
# =============================================================
def setup_logging(level: str = "INFO", log_format: str = "text") -> None:
    """
    初始化全局 logging 配置

    Args:
        level: DEBUG / INFO / WARNING / ERROR
        log_format: "json"（生产 / 日志聚合）或 "text"（开发 / 终端可读）
    """
    # 清空 root handler（避免重复）
    root = logging.getLogger()
    root.handlers.clear()

    # 选择 formatter
    if log_format == "json":
        formatter = JSONFormatter()
    else:
        formatter = TextFormatter()

    # stdout handler（容器化时 stdout 被 docker logs 收集）
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())  # 注入上下文

    root.addHandler(handler)
    root.setLevel(level.upper())

    # 静默 uvicorn 默认 access log（我们用自己的 access log）
    logging.getLogger("uvicorn.access").handlers.clear()
    logging.getLogger("uvicorn.access").propagate = False

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        f"logging 配置完成: level={level.upper()}, format={log_format}"
    )