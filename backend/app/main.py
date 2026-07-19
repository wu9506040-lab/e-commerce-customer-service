"""
智能客服 Agent 系统 - FastAPI 后端入口

M8：结构化日志 + Request ID 中间件 + /metrics 端点
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.admin_analytics import router as admin_analytics_router  # P4-2 admin 运营聚合
from app.api.admin_conversations import router as admin_conversations_router  # P4-1 admin 全局会话查询
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router  # §12 会话历史读取层
from app.api.intent import router as intent_router  # M3 意图分类
from app.api.middleware import RateLimitMiddleware, RequestIdMiddleware, ResponseHeaderMiddleware  # M8 + P0-I
from app.api.public import router as public_router  # 公开 demo 站点入口（M13 cloud）
from app.api.shop import router as shop_router  # 前端商品橱窗 + 我的订单（M9）
from app.clients.mysql_client import close_engine, get_engine
from app.clients.qdrant import _qdrant_breaker  # M8：metrics 用
from app.core.config import settings
from app.core.logging import setup_logging  # M8
from app.models import Base  # 触发 ORM 注册（确保 create_all 找到所有表）

# =============================================================
# 日志配置（M8：结构化 JSON / text 双模式）
# =============================================================
# dev 环境用 text（人类可读），prod 用 json（日志聚合系统）
LOG_FORMAT = "json" if settings.APP_ENV in ("prod", "production") else "text"
setup_logging(level=settings.LOG_LEVEL, log_format=LOG_FORMAT)
logger = logging.getLogger(__name__)

# =============================================================
# 应用
# =============================================================
app = FastAPI(
    title="智能客服 API",
    version="0.2.0",
    description="RAG + Agent 智能客服后端 - 含用户认证",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS（必须显式白名单，禁止 "*" + allow_credentials 组合）
# "*" + credentials 在浏览器会被拒绝（浏览器安全策略），等于埋雷
_cors_origins = [
    o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,  # 必须 True，cookie 才能跨域
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================
# P0-I：限流中间件（防 /chat / 登录被刷）
# 注意：CORS 之后注册（外层先于内层），让 OPTIONS 预检不受限流影响
# =============================================================
app.add_middleware(RateLimitMiddleware)

# =============================================================
# M8 中间件（必须在 CORS 之后注册 — FastAPI 中间件执行顺序是 LIFO）
# =============================================================
# 注意：ResponseHeaderMiddleware 先注册（外层），RequestIdMiddleware 后注册（内层）
# 实际请求流向：client → CORS → RateLimit → ResponseHeader → RequestId → router
# 实际响应流向：router → RequestId（设 ContextVar）→ ResponseHeader（读 ContextVar 写头）
app.add_middleware(ResponseHeaderMiddleware)
app.add_middleware(RequestIdMiddleware)


# =============================================================
# 路由
# =============================================================
@app.get("/")
async def root():
    """根端点 - 服务信息"""
    return {
        "service": "customer-service-api",
        "version": "0.2.0",
        "env": settings.APP_ENV,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
async def health():
    """
    健康检查端点（含 Redis / Qdrant / MySQL 状态）

    各组件独立 try/except，任一挂掉不影响其他检测。
    整体状态规则：
        - 全部 ok → "ok"
        - 任意组件 down → "degraded"
    """
    from sqlalchemy import text

    from app.clients.mysql_client import get_engine
    from app.clients.qdrant import get_client as qdrant_get
    from app.clients.redis_client import get_client as redis_get

    components: dict = {}

    # MySQL（同步 SQLAlchemy，FastAPI 自动放 default executor）
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        components["mysql"] = {"status": "ok"}
    except Exception as e:
        components["mysql"] = {"status": "down", "error": str(e)[:100]}

    # Redis
    try:
        redis_get().ping()
        components["redis"] = {"status": "ok"}
    except Exception as e:
        components["redis"] = {"status": "down", "error": str(e)[:100]}

    # Qdrant
    try:
        qdrant_get().get_collections()
        components["qdrant"] = {"status": "ok"}
    except Exception as e:
        components["qdrant"] = {"status": "down", "error": str(e)[:100]}

    # 注入断路器状态（M8）— 独立字段，不参与 overall 判定
    circuit_breaker = {
        "qdrant": _qdrant_breaker.stats(),
    }

    # 只统计 mysql / redis / qdrant 三个核心组件，circuit_breaker 是诊断信息
    overall = (
        "ok"
        if all(c["status"] == "ok" for c in (components["mysql"], components["redis"], components["qdrant"]))
        else "degraded"
    )
    return {
        "status": overall,
        "env": settings.APP_ENV,
        "version": "0.2.0",
        "components": components,
        "circuit_breaker": circuit_breaker,
    }


# =============================================================
# 路由注册（api/ 子模块）
# =============================================================
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(admin_analytics_router)  # P4-2 admin 运营聚合
app.include_router(admin_conversations_router)  # P4-1 admin 全局会话查询
app.include_router(auth_router)
app.include_router(conversations_router)  # §12
app.include_router(intent_router)  # M3
app.include_router(public_router)  # M13 cloud：公开 demo 入口
app.include_router(shop_router)  # 前端商品橱窗 + 我的订单


# =============================================================
# M8：/metrics 端点（业务指标 JSON 快照）
# =============================================================
@app.get(
    "/api/metrics",
    summary="业务指标快照（M8）",
    description="返回 chat / rag / embedding / hit@K 等指标的 JSON 快照。不引入 Prometheus，纯内存实现。",
)
async def metrics_endpoint():
    """
    业务指标端点

    返回字段：
        uptime_seconds    - 服务启动时长
        chat              - chat 调用数 / 意图分布 / 延迟分位数 / token 总量
        rag               - Qdrant 搜索成功 / 降级 / 错误计数
        embedding         - embedding 调用 / 重试 / 错误计数
        circuit_breaker   - 断路器状态
        hit_at_k          - 实时 hit@K（最近 100 次 RAG 检索窗口）
    """
    from app.services.metrics import metrics as metrics_singleton
    return metrics_singleton.snapshot(
        circuit_breaker_stats={"qdrant": _qdrant_breaker.stats()}
    )


# =============================================================
# 启动事件
# =============================================================
@app.on_event("startup")
async def startup_event():
    logger.info("=" * 60)
    logger.info("智能客服 API 启动")
    logger.info(f"环境: {settings.APP_ENV}")
    logger.info(f"日志级别: {settings.LOG_LEVEL}")
    logger.info(f"模型: {settings.QWEN_MODEL}")
    logger.info(f"千问 BaseURL: {settings.DASHSCOPE_BASE_URL}")
    if settings.QWEN_API_KEY and not settings.QWEN_API_KEY.startswith("sk-put-your-real"):
        logger.info("QWEN_API_KEY: 已配置（脱敏显示）")
    else:
        logger.warning("QWEN_API_KEY: 未配置或为占位符（/chat 端点将返回 500）")

    # 初始化 MySQL（建表兜底）
    try:
        engine = get_engine()
        Base.metadata.create_all(bind=engine)
        logger.info("MySQL: 连接 + create_all 完成")
    except Exception as e:
        logger.exception(f"MySQL 初始化失败: {e}")
        # 不 raise，让 FastAPI 启动（DB 暂时不可用不会让 API 完全挂掉）

    # Cookie 配置提示
    logger.info(
        f"Cookie: name={settings.COOKIE_NAME}, "
        f"secure={settings.COOKIE_SECURE}, "
        f"samesite={settings.COOKIE_SAMESITE}"
    )
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """优雅关闭：释放连接池"""
    close_engine()
    logger.info("MySQL engine 已关闭")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
