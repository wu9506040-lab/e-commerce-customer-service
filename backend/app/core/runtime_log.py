"""
V16.5 全链路运行时日志（用户要求补齐 L1-L8 可观测性）

设计原则（CLAUDE.md 最小修改 + 解耦）：
1. **独立 logger 名字** `xianyu_runtime` — 不污染 root logger
2. **不打 stdout**（容器内被 docker logs 吞，文件路径被覆盖）— 只写文件
3. **不打数据库**（避免 hot path 延迟）
4. **文件**: `logs/xianyu_runtime.log`（按天滚动, 保留 7 天 backupCount=7）
5. **格式**: `[时间戳] | LEVEL | cid=xxx | unb=xxx | stage=xxx | 内容`
6. **propagate=False** 防止 root logger handler 重复输出

L 点覆盖（V16.5 全链路追踪）：
- L1  _handle_product 双空兜底 (orchestrator.py)
- L2  send_message 失败 (xianyu_mcp_client.py)
- L4  RAG 检索 Top-3 + scores (orchestrator.py)
-    Prompt/Response 日志 (orchestrator.py)
-    Intent 分类结果 (orchestrator.py)
- L5  SSE 事件流汇总 (chat.py:_event_gen 结尾)
- L6  Rate-limit 拒绝详情 (reply_scheduler.py)
- L7  BuyerProfile cache hit/miss (xianyu_buyer_profile_v2.py)
- L8  YAML 模板加载状态 (xianyu_prompts.py)

启动期调用约定：
  main.py 已调 setup_logging()，本模块在 setup_logging() 末尾追加一行调用即可
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

CST = timezone(timedelta(hours=8))


class RuntimeContextFilter(logging.Filter):
    """保证每条日志有 cid/unb/stage 字段，避免 Formatter KeyError"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "cid"):
            record.cid = "-"
        if not hasattr(record, "unb"):
            record.unb = "-"
        if not hasattr(record, "stage"):
            record.stage = "-"
        return True


# 用户指定格式（CLAUDE.md 提交约束）
_RUNTIME_FORMAT = (
    "[%(asctime)s] | %(levelname)-7s | cid=%(cid)s | unb=%(unb)s | stage=%(stage)s | %(message)s"
)
_RUNTIME_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 单例
_runtime_logger: logging.Logger | None = None
_initialized: bool = False


def setup_runtime_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """初始化运行时文件 logger（启动期调一次，安全可重入）

    Args:
        level:  最低输出级别（INFO / WARNING / ERROR）
        log_dir: 日志目录（默认相对路径 logs/，user 指定不改）
    """
    global _runtime_logger, _initialized
    if _initialized:
        return

    # 1. 路径解析（相对路径相对当前 cwd；不强制绝对路径 — 兼容 docker 部署）
    log_path = Path(log_dir)
    if not log_path.is_absolute():
        log_path = Path(os.getcwd()) / log_path
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "xianyu_runtime.log"

    # 2. 文件 handler（midnight 滚动 + 7 天 backup）
    handler = TimedRotatingFileHandler(
        str(log_file), when="midnight", backupCount=7, encoding="utf-8"
    )
    handler.suffix = "%Y%m%d"  # 备份文件名 xianyu_runtime.log.20261015
    handler.setFormatter(logging.Formatter(_RUNTIME_FORMAT, _RUNTIME_DATEFMT))
    handler.addFilter(RuntimeContextFilter())

    # 3. logger 单例
    _runtime_logger = logging.getLogger("xianyu_runtime")
    _runtime_logger.setLevel(level.upper())
    _runtime_logger.addHandler(handler)
    _runtime_logger.propagate = False  # 关键：不向 root 传播（不打 stdout）
    _initialized = True

    _runtime_logger.info(
        "xianyu_runtime logging init OK file=" + str(log_file),
        extra={"cid": "startup", "unb": "startup", "stage": "INIT"},
    )


def log_event(
    stage: str,
    level: str,
    cid: str,
    unb: str,
    message: str,
    **extras: object,
) -> None:
    """统一入口：各业务模块调一次即可

    Args:
        stage:   当前阶段名（INTENT_CLASSIFY / RAG_RETRIEVE / LLM_PROMPT / SSE_SUMMARY ...）
        level:   INFO / WARNING / ERROR（大小写不限）
        cid:     会话 ID（闲鱼 cid 或 chat session_id）
        unb:     买家 ID（闲鱼 user_id 或买家标识）
        message: 日志主要文本（单行）
        **extras: 任意结构化字段（latency_ms / tokens / score / error_type ...）
                  自动拼到 message 末尾，格式 [k1=v1 k2=v2]
    """
    if _runtime_logger is None:
        return  # 未初始化（容器已退出 / 单元测试未启动 setup）—— 静默 no-op
    extra = {
        "cid": cid or "-",
        "unb": unb or "-",
        "stage": stage,
    }
    extra.update(extras)

    # 自动把 extras 拼到 message 末尾（不依赖 Formatter 改）
    if extras:
        parts = []
        for k, v in extras.items():
            if callable(v):
                continue
            s = str(v)
            # 截断长值（堆栈 / 文本预览等）
            if len(s) > 300:
                s = s[:300] + "...(truncated)"
            parts.append(f"{k}={s}")
        if parts:
            message = f"{message} [{' '.join(parts)}]"

    _runtime_logger.log(
        getattr(logging, level.upper(), logging.INFO),
        message,
        extra=extra,
    )


__all__ = ["setup_runtime_logging", "log_event"]
