"""
HTTP 中间件 — M8 可观测性

RequestIdMiddleware：
- 提取/生成 X-Request-Id（透传 / 自动生成 UUID4）
- 写入 ContextVar（让日志自动带 request_id）
- 记录访问日志：method / path / status / duration_ms
- 响应头回写 X-Request-Id（让客户端能追踪）

设计要点：
- 用 BaseHTTPMiddleware（FastAPI 标准接口）
- 请求结束务必 reset ContextVar（避免跨请求污染）
- access log 用 INFO 级别（业务日志 WARNING+ 默认不显）
- /health / /metrics / /docs 不记日志（避免噪音）
"""
import logging
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.core.context import (
    request_id_var,
    reset_request_id,
    set_request_id,
)

logger = logging.getLogger(__name__)

# 不记日志的路径（健康检查 / 监控 / OpenAPI）
_SKIP_LOG_PATHS = {"/health", "/api/metrics", "/docs", "/openapi.json", "/redoc"}
# Header 名
REQUEST_ID_HEADER = "X-Request-Id"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """请求 ID 中间件 + 访问日志"""

    async def dispatch(self, request: Request, call_next) -> Response:
        # 1. 提取 / 生成 request_id
        # 优先用客户端传入的（多服务串联场景），否则生成 UUID4
        incoming_rid = request.headers.get(REQUEST_ID_HEADER)
        rid = incoming_rid if incoming_rid else f"req-{uuid.uuid4().hex[:16]}"

        # 2. 写入 ContextVar（所有后续 logger 自动带 rid）
        rid_token = set_request_id(rid)

        # 3. 计时
        start = time.perf_counter()
        status_code = 500  # 默认值，万一 call_next 抛异常
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            # 4. 计算耗时 + 访问日志
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            path = request.url.path
            if path not in _SKIP_LOG_PATHS:
                # 用 logger.info 自动带 request_id（ContextFilter 已注入）
                logger.info(
                    f"{request.method} {path} {status_code}",
                    extra={
                        "method": request.method,
                        "path": path,
                        "status": status_code,
                        "duration_ms": duration_ms,
                        "client": request.client.host if request.client else None,
                    },
                )
            # 5. 清理 ContextVar（必须，避免污染下一个请求）
            reset_request_id(rid_token)


class ResponseHeaderMiddleware(BaseHTTPMiddleware):
    """把 X-Request-Id 写回响应头（让客户端能拿到）"""

    async def dispatch(self, request: Request, call_next) -> Response:
        # 等 RequestIdMiddleware 先设置好 ContextVar
        response = await call_next(request)
        rid = request_id_var.get()
        if rid and rid != "-":
            response.headers[REQUEST_ID_HEADER] = rid
        return response


# =============================================================
# P0-I：基础限流中间件（不引第三方依赖，纯内存固定窗口）
# =============================================================
# 触发场景：
# - 公网 demo 站点经常被脚本刷 /chat 烧 token
# - /auth/login 被暴力破解撞库
#
# 设计：
# - 固定窗口计数器（按 IP + 路径桶分），内存 dict 存储
# - 单进程够用；多实例请接 Redis
# - 白名单路径（/health / /metrics）不限流
# - 超限返 429 + Retry-After
import time as _time
from collections import deque

# 限流配置：(路径前缀, 每分钟上限)
_RATE_LIMIT_RULES = [
    ("/api/chat", 30),           # 对齐 RATE_LIMIT_PER_MINUTE 默认值
    ("/api/auth/login", 10),     # 登录更严，防撞库
    ("/api/auth/register", 5),   # 注册更严，防刷号
    ("/api/public/demo-account", 10),  # 一键 demo 不能让单 IP 反复刷
]

# 不限流的路径
_RATE_LIMIT_SKIP_PATHS = {"/health", "/api/metrics", "/docs", "/openapi.json", "/redoc", "/"}


class _FixedWindowCounter:
    """固定窗口计数器：deque 存请求时间戳，超出窗口的弹出"""
    __slots__ = ("_hits",)

    def __init__(self) -> None:
        self._hits: deque[float] = deque()

    def hit_and_check(self, limit: int, window_sec: int = 60) -> tuple[bool, int]:
        """记录一次命中，返回 (是否允许, 重试等待秒数)

        允许：返回 (True, 0)
        拒绝：返回 (False, retry_after_sec) — 距窗口清空还需多久
        """
        now = _time.monotonic()
        cutoff = now - window_sec
        # 弹出窗口外的旧记录
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= limit:
            # 最早一条何时离开窗口 = retry_after
            retry_after = max(1, int(self._hits[0] + window_sec - now) + 1)
            return False, retry_after
        self._hits.append(now)
        return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """P0-I：按 (IP, 路径前缀) 桶分限流

    注意：
    - 单进程内存版，重启计数清零；多实例部署需替换为 Redis 版
    - 匹配最具体的路径前缀（先匹配长前缀）
    """

    def __init__(self, app, rules=None, skip_paths=None):
        super().__init__(app)
        # 排序规则：路径前缀长的优先匹配
        self._rules = sorted(
            rules or _RATE_LIMIT_RULES,
            key=lambda x: len(x[0]),
            reverse=True,
        )
        self._skip_paths = set(skip_paths or _RATE_LIMIT_SKIP_PATHS)
        # key → _FixedWindowCounter
        self._buckets: dict[tuple[str, str], _FixedWindowCounter] = {}

    @staticmethod
    def _client_ip(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _match_rule(self, path: str) -> int | None:
        """返回路径命中的限流上限；不匹配返回 None"""
        if path in self._skip_paths:
            return None
        for prefix, limit in self._rules:
            if path.startswith(prefix):
                return limit
        return None

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        limit = self._match_rule(path)
        if limit is None:
            return await call_next(request)
        ip = self._client_ip(request)
        key = (ip, path)
        counter = self._buckets.get(key)
        if counter is None:
            counter = _FixedWindowCounter()
            self._buckets[key] = counter
        allowed, retry_after = counter.hit_and_check(limit)
        if not allowed:
            from starlette.responses import JSONResponse
            logger.warning(
                f"rate limit exceeded: ip={ip} path={path} limit={limit}/min",
                extra={"ip": ip, "path": path, "limit": limit},
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"请求过于频繁，{retry_after} 秒后再试",
                    "limit_per_minute": limit,
                    "retry_after_sec": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)