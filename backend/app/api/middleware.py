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