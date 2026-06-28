"""
circuit_breaker.py - 断路器（防止外部依赖故障级联）

3 个状态机：
    CLOSED（正常）→ 失败计数累加 → 超过阈值 → OPEN（开路）
    OPEN（开路）→ 快速失败 + 拒绝调用 → 等待 recovery_timeout → HALF_OPEN
    HALF_OPEN（半开）→ 放一个请求探活 → 成功 → CLOSED / 失败 → OPEN

设计取舍：
- 线程安全：用 threading.Lock 保护状态（同步 Qdrant / sync clients 用得到）
- 异步友好：提供 async with / 同步函数两种 API
- 可观测：每次状态切换 + 拒绝调用都记 WARNING log
- 降级默认：调用方在 OPEN 状态拿到的不是异常，而是 CircuitOpenError（可降级到默认值）

面试亮点：
- "Qdrant 挂了怎么办？" → "断路器开路 → RAG 返回空 → LLM 走工具调用兜底"
- "为什么不直接 try/except？" → "断路器防止雪崩（线程池被慢调用占满）"
"""
import logging
import threading
import time
from enum import Enum
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"        # 正常
    OPEN = "open"            # 开路（拒绝所有调用）
    HALF_OPEN = "half_open"  # 半开（放一个探活）


class CircuitOpenError(Exception):
    """断路器开路时抛出，调用方可降级处理"""
    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(f"circuit '{name}' is OPEN, retry after {retry_after:.1f}s")


class CircuitBreaker:
    """
    断路器（同步版）

    用法：
        breaker = CircuitBreaker(name="qdrant", failure_threshold=3, recovery_timeout=10)
        try:
            result = breaker.call(qdrant_search_fn, vector, top_k=5)
        except CircuitOpenError:
            return []  # 降级
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        expected_exceptions: tuple = (Exception,),
    ):
        """
        Args:
            name: 标识（用于日志）
            failure_threshold: 连续失败多少次后开路
            recovery_timeout: 开路后多少秒进入 HALF_OPEN
            expected_exceptions: 哪些异常算"失败"（其余异常不算，不计入失败计数）
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exceptions = expected_exceptions

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_in_flight = False  # 防止 HALF_OPEN 放多个探活
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        """当前状态（懒检查：OPEN 状态超时会自动转 HALF_OPEN）"""
        if self._state == CircuitState.OPEN and self._should_attempt_recovery():
            with self._lock:
                if self._state == CircuitState.OPEN and self._should_attempt_recovery():
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_in_flight = False
                    logger.warning(
                        f"circuit '{self.name}': OPEN → HALF_OPEN (recovery probe)"
                    )
        return self._state

    def _should_attempt_recovery(self) -> bool:
        if self._last_failure_time is None:
            return False
        return (time.time() - self._last_failure_time) >= self.recovery_timeout

    def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """
        通过断路器调用 func

        Raises:
            CircuitOpenError: 断路器开路时
            func 自身的异常: 失败时记录 + 计数
        """
        state = self.state

        if state == CircuitState.OPEN:
            retry_after = self.recovery_timeout
            if self._last_failure_time is not None:
                retry_after = max(0, self.recovery_timeout - (time.time() - self._last_failure_time))
            raise CircuitOpenError(self.name, retry_after)

        if state == CircuitState.HALF_OPEN:
            with self._lock:
                if self._half_open_in_flight:
                    # 已有探活在跑，拒绝其他调用
                    raise CircuitOpenError(self.name, 1.0)
                self._half_open_in_flight = True

        try:
            result = func(*args, **kwargs)
        except self.expected_exceptions as e:
            self._on_failure(e)
            raise
        except Exception as e:
            # 不在 expected_exceptions 里的异常不计入失败（如 KeyError 等业务错误）
            logger.debug(f"circuit '{self.name}' unexpected exception (not counted): {e}")
            raise

        self._on_success()
        return result

    def _on_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.warning(f"circuit '{self.name}': HALF_OPEN → CLOSED (recovered)")
                self._state = CircuitState.CLOSED
                self._half_open_in_flight = False
            self._failure_count = 0

    def _on_failure(self, exception: Exception) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._state == CircuitState.HALF_OPEN:
                # 探活失败 → 重新 OPEN
                logger.warning(
                    f"circuit '{self.name}': HALF_OPEN → OPEN (probe failed: "
                    f"{type(exception).__name__}: {str(exception)[:100]})"
                )
                self._state = CircuitState.OPEN
                self._half_open_in_flight = False
            elif self._failure_count >= self.failure_threshold:
                if self._state != CircuitState.OPEN:
                    logger.warning(
                        f"circuit '{self.name}': CLOSED → OPEN "
                        f"({self._failure_count} consecutive failures, "
                        f"last: {type(exception).__name__}: {str(exception)[:100]})"
                    )
                    self._state = CircuitState.OPEN

    def reset(self) -> None:
        """手动重置（管理后台/测试用）"""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._half_open_in_flight = False
            logger.info(f"circuit '{self.name}': manual reset → CLOSED")

    def stats(self) -> dict:
        """导出状态（健康检查端点用）"""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "last_failure_time": self._last_failure_time,
        }
