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
    DATABASE_URL: str = (
        "mysql+pymysql://cs_user:dev_user_2026@mysql:3306/"
        "customer_service?charset=utf8mb4"
    )

    # ---- JWT ----
    JWT_SECRET: str = "change_me_at_least_32_chars_random_xxx_string_here"
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


settings = Settings()
