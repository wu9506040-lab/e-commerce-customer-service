"""
智能客服 Agent 系统 - FastAPI 后端入口
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router  # §12 会话历史读取层
from app.clients.mysql_client import close_engine, get_engine
from app.core.config import settings
from app.models import Base  # 触发 ORM 注册（确保 create_all 找到所有表）

# =============================================================
# 日志配置
# =============================================================
logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
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

# CORS（开发模式全开，生产用具体域名）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.APP_ENV == "dev" else [],
    allow_credentials=True,  # 必须 True，cookie 才能跨域
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    overall = (
        "ok"
        if all(c["status"] == "ok" for c in components.values())
        else "degraded"
    )
    return {
        "status": overall,
        "env": settings.APP_ENV,
        "version": "0.2.0",
        "components": components,
    }


# =============================================================
# 路由注册（api/ 子模块）
# =============================================================
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(conversations_router)  # §12


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
