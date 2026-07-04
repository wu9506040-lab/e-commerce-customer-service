"""
应用配置 - 统一从环境变量读取（pydantic-settings）

按 §6 规则：core/ 层核心能力，提供全局配置
- Docker 容器内：不读 .env，靠 docker-compose.yml 的 environment 注入
- 本地开发：自动读项目根 .env（如果有）
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- MySQL ----
    # 强制从环境变量读取，禁止硬编码凭据
    # 启动时由 _validate_database_url() 校验：未设置或包含已知占位符密码则 ValueError
    DATABASE_URL: str = ""

    # ---- JWT ----
    # 强制从环境变量读取，禁止占位符默认值
    # 启动时由 _validate_jwt_secret() 校验：未设置或使用占位符则 ValueError
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24

    # ---- bcrypt ----
    BCRYPT_ROUNDS: int = 12

    # ---- 应用 ----
    APP_ENV: str = "dev"

    # ---- Cookie ----
    # production 必须 True（要求 HTTPS），dev False 方便 curl/Postman 调试
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: str = "lax"
    COOKIE_NAME: str = "cs_token"
    COOKIE_MAX_AGE: int = 86400  # 24h，与 JWT_EXPIRE_HOURS 对齐

    # ---- 外部服务 ----
    REDIS_URL: str = "redis://localhost:6379/0"
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "knowledge_base"

    # ---- LLM (DashScope OpenAI 兼容) ----
    QWEN_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_MODEL: str = "qwen-max"

    # ---- 日志 ----
    LOG_LEVEL: str = "INFO"

    # ---- V3 LangGraph 开关（默认关闭，需手动设 true 启用）----
    # 控制 synthesizer._handle_refund 走 V2.x service 版还是 V3 LangGraph 版
    USE_LANGGRAPH_REFUND: bool = False

    # ---- Demo 访客登录（公开 demo 站点用）----
    # True 时开放 /api/public/demo-account 一键体验；生产默认开启（这是简历展示用项目）
    # 安全策略：每次创建一个隔离的 visitor_xxx 用户，30 天过期自动清理
    ENABLE_DEMO_LOGIN: bool = True

    # ---- 速率限制（开放 demo 必加，防止被刷 token）----
    RATE_LIMIT_PER_MINUTE: int = 30

    # ---- CORS（不允许 "*"+credentials 组合，强制显式 origin 白名单）----
    # dev: ["http://localhost:5173","http://127.0.0.1:5173","http://120.79.27.124:5173"]
    # prod: ["http://120.79.27.124:5173"]
    # 用逗号分隔的字符串写入环境变量，逗号分隔解析成 list
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"


settings = Settings()


def _validate_jwt_secret() -> None:
    """启动时校验 JWT_SECRET：必须 ≥32 字符且不是占位符

    为什么：占位符或弱密钥等于无签名校验，攻击者可伪造任意 token
    """
    _PLACEHOLDERS = {
        "change_me_at_least_32_chars_random_xxx_string_here",
        "",
        "secret",
        "changeme",
    }
    secret = settings.JWT_SECRET
    if secret in _PLACEHOLDERS:
        raise ValueError(
            "JWT_SECRET 未设置或使用占位符默认值。\n"
            "请在 deploy/.env.dev（或部署环境）设置 JWT_SECRET=<至少 32 字符随机字符串>\n"
            "生成命令：python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(secret) < 32:
        raise ValueError(
            f"JWT_SECRET 长度 {len(secret)} < 32 字符，强度不足。"
            "请用 secrets.token_hex(32) 生成。"
        )


_validate_jwt_secret()


def _validate_database_url() -> None:
    """启动时校验 DATABASE_URL：必须设置且不含已知占位符密码

    为什么：默认值带密码会让 docker-compose 不显式注入时也能"看似启动"，给运维错觉

    注意：dev 环境允许 .env.dev 里的弱密码占位符（dev_user_2026 等），
         因为 .env.dev 不进 Git 且明确写"开发用"。
         prod 环境必须用 secrets manager 注入真实强密码，禁止占位符。
    """
    _PLACEHOLDER_PWD_FRAGMENTS_DEV = ()  # dev 环境允许 .env.dev 弱密码
    _PLACEHOLDER_PWD_FRAGMENTS_PROD = ("dev_user_2026", "rootpass_cs_2026", "change_me", "password", "secret")
    url = settings.DATABASE_URL
    if not url:
        raise ValueError(
            "DATABASE_URL 未设置。\n"
            "请在 deploy/.env.dev（或部署环境）设置：\n"
            "DATABASE_URL=mysql+pymysql://cs_user:<密码>@mysql:3306/customer_service?charset=utf8mb4"
        )
    # prod / production 环境必须严格：禁止任何占位符密码
    if settings.APP_ENV in ("prod", "production"):
        for frag in _PLACEHOLDER_PWD_FRAGMENTS_PROD:
            if frag in url:
                raise ValueError(
                    f"APP_ENV={settings.APP_ENV} 环境下，DATABASE_URL 含占位符密码 '{frag}'。"
                    "生产环境必须从 secrets manager 注入真实密码。"
                )


_validate_database_url()
