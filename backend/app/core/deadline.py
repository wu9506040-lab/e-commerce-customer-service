"""V14（3.3）请求级超时预算（deadline budget）

问题（OPTIMIZATION-2026-10 §3.3 实测）：单阶段最坏尾部层层叠——LLM 单次 30s
超时 × (1+3 次重试) + 退避 ≈ 130s，rerank/意图/生成串行叠加历史最坏可到分钟级，
用户侧没有任何"这一轮最多多久"的承诺。

方案：入口处 `start_budget(seconds)` 把绝对截止时刻写进 contextvar；
所有 LLM/embedding/rerank 调用在 client 层统一做两件事——
1. `clamp_timeout(default)`：单次 HTTP 超时 = min(自身上限, 剩余预算)，
   预算耗尽前绝不会发出"注定超窗"的长调用；
2. `check()`：重试循环/排队/流式推进的每个关口过一遍，超线立即抛
   `DeadlineExceededError` 让上游走既有降级矩阵（快速失败显形，不硬撑）。

contextvar 随请求任务生命周期消亡，`asyncio.to_thread` 复制上下文
（2.2 embed memo 同一机制，已活体验证传播），阶段间零显式传参。

诚实边界：预算是"墙钟软上限"——已进入的 HTTP 调用靠 clamp 收敛，
不承诺 in-flight 请求秒杀（那要 cancel 级改造，收益不配成本）。
"""
from __future__ import annotations

import contextvars
import time
from typing import Optional

# 绝对截止时刻（time.monotonic 基准）；None = 无预算（脚本/测试/后台任务）
_deadline_at: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "request_deadline_at", default=None)


class DeadlineExceededError(Exception):
    """预算耗尽。stage 标记死在哪一段，metrics 按 stage 计数显形。"""

    def __init__(self, message: str, stage: str = "-") -> None:
        super().__init__(message)
        self.stage = stage


def start_budget(seconds: float) -> None:
    """在请求入口设置总预算（秒）。重复调用以最后一次为准（同任务内幂等覆盖）。"""
    _deadline_at.set(time.monotonic() + max(0.0, float(seconds)))


def clear_budget() -> None:
    _deadline_at.set(None)


def has_budget() -> bool:
    return _deadline_at.get() is not None


def remaining() -> Optional[float]:
    """剩余预算秒数；无预算返回 None。可能为负（已超时未检查）。"""
    at = _deadline_at.get()
    if at is None:
        return None
    return at - time.monotonic()


def clamp_timeout(default: float) -> float:
    """单次调用的有效超时 = min(default, 剩余预算)。无预算原样返回。

    至少留 0.5s 地板：避免传 0 给 httpx 变成"立即失败"的另一种静默。"""
    rem = remaining()
    if rem is None:
        return default
    return max(0.5, min(default, rem))


def check(stage: str) -> None:
    """关口检查：预算已尽则抛 DeadlineExceededError。"""
    rem = remaining()
    if rem is not None and rem <= 0:
        raise DeadlineExceededError(
            f"budget exhausted before stage={stage} (remaining={rem:.1f}s)",
            stage=stage)


def can_afford(cost_seconds: float) -> bool:
    """还能不能撑起 cost_seconds 的下一步（退避等待/整次调用）。无预算恒真。"""
    rem = remaining()
    return rem is None or rem > cost_seconds
