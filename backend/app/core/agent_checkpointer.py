"""Agent 记忆持久化：LangGraph Checkpointer 工厂（V20 亮点 1）

为什么需要：
- refund_graph 的 workflow.compile() 此前未传 checkpointer——图一旦中断
  （进程重启 / 异常），执行到一半的状态即丢失，escalate 前的决策无法审计。
- 接入 checkpointer 后：① 未完成流程在进程重启后可查可续跑（容灾恢复）
  ② 任意会话的图状态历史可回放（决策审计）。

实现分级（依赖缺失时自动降级，不阻塞启动）：
- sqlite（默认）：langgraph-checkpoint-sqlite 的 SqliteSaver，落盘可恢复
- memory：进程内 MemorySaver（测试用 / sqlite 依赖缺失时的兜底）
- none：与旧行为一致（不启用）

约定：checkpointer 是优化项不是阻塞项——任何初始化失败都降级到 memory
并打 warning，绝不让 Agent 主链路因持久化层挂掉。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_checkpointer = None  # 模块级单例（进程内共享；多线程经 thread_id 隔离）


def get_checkpointer():
    """返回 LangGraph checkpointer 单例（按 settings.AGENT_CHECKPOINTER_TYPE）。

    返回值可直接传给 workflow.compile(checkpointer=...)；
    返回 None 表示不启用（type=none 或初始化失败），调用方保持旧行为。
    """
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    with _lock:
        if _checkpointer is not None:
            return _checkpointer

        from app.core.config import settings

        ctype = (getattr(settings, "AGENT_CHECKPOINTER_TYPE", "sqlite") or "none").lower()

        if ctype == "none":
            logger.info("[checkpointer] type=none，不启用 Agent 状态持久化")
            return None

        if ctype == "sqlite":
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver

                db_path = Path(getattr(settings, "AGENT_CHECKPOINTER_DB", "data/agent_checkpoints.sqlite3"))
                db_path.parent.mkdir(parents=True, exist_ok=True)
                # check_same_thread=False：FastAPI 多 worker 线程共享一个连接，
                # 写并发由 sqlite 自身锁保护（checkpointer 写入频次低，够用）
                conn = sqlite3.connect(str(db_path), check_same_thread=False)
                _checkpointer = SqliteSaver(conn)
                logger.info("[checkpointer] SqliteSaver 就绪: %s", db_path)
                return _checkpointer
            except ImportError:
                logger.warning(
                    "[checkpointer] langgraph-checkpoint-sqlite 未安装，降级为 memory。"
                    "持久化恢复需: pip install langgraph-checkpoint-sqlite"
                )
            except Exception:  # noqa: BLE001 —— 任何初始化异常都不应阻塞 Agent 启动
                logger.exception("[checkpointer] SqliteSaver 初始化失败，降级为 memory")

        # memory（显式配置 or 上面的降级路径）
        try:
            from langgraph.checkpoint.memory import MemorySaver

            _checkpointer = MemorySaver()
            logger.info("[checkpointer] MemorySaver 就绪（仅进程内，重启不恢复）")
        except Exception:  # noqa: BLE001
            logger.exception("[checkpointer] MemorySaver 亦不可用，Agent 状态不持久化")
            _checkpointer = None
        return _checkpointer


def reset_checkpointer() -> None:
    """测试辅助：清空单例，让下一个用例重新按 settings 初始化。"""
    global _checkpointer
    with _lock:
        _checkpointer = None


def build_thread_id(
    session_id: str | None, user_id: int, order_no: str | None, query: str
) -> str:
    """构造退款图的 thread_id。

    有 session_id：`refund:s:{session}:{user}:{order|qhash}` ——
    同一会话内同一订单共享一条轨迹，中断可续跑（断点续跑语义）。

    无 session_id（测试 / 旧调用方直连）：`refund:a:{uuid}` ——
    一次性轨迹：checkpoint 照常写入（决策审计可用），但永不跨调用续跑，
    避免固定 mock 数据在不同调用间串状态。
    """
    if not session_id:
        import uuid

        return f"refund:a:{uuid.uuid4().hex[:12]}"

    if order_no:
        return f"refund:s:{session_id}:{user_id}:{order_no}"
    import hashlib

    qhash = hashlib.md5(query.strip().encode("utf-8")).hexdigest()[:10]
    return f"refund:s:{session_id}:{user_id}:q{qhash}"
