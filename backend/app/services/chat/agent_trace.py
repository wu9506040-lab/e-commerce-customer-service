"""Agent 决策追踪（V20 亮点 3）：每轮对话一条结构化轨迹。

为什么需要：
- 此前 Agent 的路由 / 重试 / 降级信息散落在日志与 metrics 里，无法按会话回放，
  也算不出"自动处理率 / 转人工率"这类业务价值指标（亮点 4 的数据源）。
- 设计取舍：不做 OpenTelemetry（重、引依赖），一期用 JSONL 落盘——
  一轮一行 JSON，grep/jq 即可分析；后续量大再换 MySQL/ClickHouse，字段结构不变。

可靠性约定（与 checkpointer 同一哲学）：
- trace 是观测项不是业务项——**flush 里的任何异常都吞掉打 warning**，
  绝不影响主应答链路。
- 同步写（asyncio.to_thread 由调用方决定），文件 append 模式，多进程安全靠
  单行 O_APPEND 原子性（< PIPE_BUF 一行不会被截断）。

一条 trace 的字段（一问一答一轮）：
  ts, session_id, user_id, channel, query_len,
  intent, intent_method, intent_confidence,     # 意图路由
  cache_hit, degraded, outcome,                 # 路径与结果
  latency_ms, error_type
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()


class AgentTrace:
    """单轮对话的轨迹收集器：chat 主流程各节点 mark，结束统一 flush。"""

    __slots__ = (
        "_data", "_start", "_done",
    )

    def __init__(self, session_id: str, user_id: Any, channel: Optional[str], query: str):
        from app.core.config import settings

        self._start = time.perf_counter()
        self._done = False
        self._data: dict[str, Any] = {
            "ts": int(time.time() * 1000),
            "session_id": session_id,
            "user_id": user_id,
            "channel": channel or "web",
            "query_len": len(query or ""),
            "trace_enabled": bool(getattr(settings, "AGENT_TRACE_ENABLED", True)),
        }

    # ---------- 各阶段 mark（全部 best-effort，不抛异常） ----------

    def mark(self, key: str, value: Any) -> None:
        """记录一个字段（intent / degraded / cache_hit ...）。"""
        try:
            self._data[key] = value
        except Exception:  # pragma: no cover —— __slots__/序列化异常兜底
            pass

    def mark_intent(self, snap: Optional[dict]) -> None:
        """记录意图路由快照（chat.py 消费的 _intent 内部事件）。"""
        if not snap:
            return
        try:
            self._data["intent"] = snap.get("intent") or snap.get("primary")
            self._data["intent_method"] = snap.get("method")
            self._data["intent_confidence"] = snap.get("confidence")
            if snap.get("upgraded"):
                self._data["intent_upgraded"] = True
        except Exception:  # noqa: BLE001
            pass

    # ---------- flush：唯一落盘点 ----------

    def flush(self, outcome: str, error_type: Optional[str] = None) -> None:
        """把轨迹写入 JSONL。任何异常吞掉——观测层绝不影响应答。"""
        if self._done:
            return
        self._done = True
        try:
            from app.core.config import settings

            if not getattr(settings, "AGENT_TRACE_ENABLED", True):
                return

            self._data["outcome"] = outcome
            if error_type:
                self._data["error_type"] = error_type
            self._data["latency_ms"] = round((time.perf_counter() - self._start) * 1000, 1)

            trace_dir = Path(getattr(settings, "AGENT_TRACE_DIR", "data/agent_traces"))
            trace_dir.mkdir(parents=True, exist_ok=True)
            path = trace_dir / f"trace-{time.strftime('%Y%m%d')}.jsonl"

            line = json.dumps(self._data, ensure_ascii=False, default=str)
            with _write_lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 —— 观测层异常只打日志
            logger.warning("[agent_trace] flush 失败（忽略）: %s", e)
