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

    # ---- Prompt 加载器（Sprint 2）----
    # 相对 backend 根目录的路径；绝对路径可直接覆盖
    # 例如：容器内 /app/config/prompts → PROMPT_DIR=/app/config/prompts
    # docker compose 默认 cwd=/app，相对路径 "config/prompts" 即可（推荐）
    PROMPT_DIR: str = "config/prompts"

    # ---- 业务规则加载器（Sprint 4）----
    # 相对 backend 根目录的路径；绝对路径可直接覆盖
    # 例如：容器内 /app/config/business_rules → BUSINESS_RULES_DIR=/app/config/business_rules
    # 业务规则启动时加载，不参与热更新（roadmap §3.5：改规则需重启服务）
    BUSINESS_RULES_DIR: str = "config/business_rules"

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

    # ---- 两阶段检索：粗排 top-K → LLM rerank → 精排 top-k ----
    # 开启后 PolicyService.search_policy 走 Qdrant top-15 → rerank → top-3
    # 关闭则保持原样（直接 Qdrant top-3）
    # 成本：每次 policy_query 多 ~500 token（rerank 调用）
    # 收益：长尾召回 top-1 准确率 +15-25%
    USE_RERANK: bool = True
    # 粗排候选数（rerank.py 单 prompt 上限 15）
    RERANK_CANDIDATE_TOP_K: int = 15

    # ---- 混合检索：vector (dense) + BM25 (sparse) + RRF ----
    # 开启后 PolicyService.search_policy 走 hybrid 路径：
    #   vector top-K + BM25 top-K → RRF 融合 → rerank → top-k
    # 关闭则仅用 vector 检索（保持单一 dense 路径）
    # 成本：BM25 索引启动时一次性构建（<1s for 67 docs），查询时几乎零开销（纯 Python）
    # 收益：解决"关键词精确命中但语义不相似"的召回盲区
    # 典型案例：用户搜 "ZP2 Pro Max 续航"，BM25 召回精确含型号的 doc，
    #          vector 召回含"续航"语义的 doc，融合后两者都进 top
    USE_HYBRID_BM25: bool = True
    # BM25 候选数（与 vector 粗排一致，保证融合时两路平衡）
    BM25_TOP_K: int = 15
    # RRF k 常数（Cormack 2009 论文推荐 60）
    RRF_K: int = 60

    # ---- Phase 4 A4: Multi-Query 检索增强 ----
    # 启用后 query_rewriter 输出 N 路变体 → policy_service RRF 融合检索
    # 默认 false（灰度用），改 settings 不需改代码
    ENABLE_MULTI_QUERY: bool = False
    # 变体数量上限（N ≤ 3 控 Qdrant 调用次数 ×N 的成本）
    MULTI_QUERY_COUNT: int = 3
    # 触发条件（MVP: coref_only；扩展: any / never）
    MULTI_QUERY_TRIGGER: str = "coref_only"

    # ---- P2 长程记忆：跨 session 用户画像 ----
    # 启用后 orchestrator 每轮加载 user_profiles，注入到 LLM prompt 的 context_block 之后
    # 默认 false（灰度用），未启用时所有 profile_service 调用短路返空
    ENABLE_USER_PROFILE: bool = False
    # profile_block 注入 prompt 的硬上限（防 prompt 膨胀；与 prompt_assembler MAX_PROFILE_PROMPT_LEN 对齐）
    USER_PROFILE_PROMPT_MAX_LEN: int = 200

    # ---- Sprint 5: Prompt 版本管理（manifest 模式）----
    # 启用后 prompt_loader 支持 load(name, version=...) 多版本接口
    # 关闭则所有调用走 manifest 的 default_version（行为与旧版完全一致）
    # 默认 false（灰度用），迁移完成后再开 true
    ENABLE_PROMPT_VERSIONING: bool = False

    # ---- LLM 客户端：retry + 指数退避 + 断路器 ----
    # 解决现网抖动：DashScope 5xx / 网络超时 / 偶发 429 时不直接降级到兜底文本
    # 而是重试 N 次（指数退避 + 抖动），仍失败则断路器开路避免雪崩
    # 可重试错误：429 / 5xx / Timeout / ConnectionError
    # 不可重试：400 / 401 / 403（业务错，重试无意义）
    # 总尝试次数 = LLM_MAX_RETRIES + 1（含首次）
    LLM_MAX_RETRIES: int = 3
    # 退避基础延迟（秒）：wait = base * 2^attempt + jitter(0-50%)
    LLM_RETRY_BASE_DELAY: float = 1.0
    # 断路器：连续失败 N 次开路
    LLM_CIRCUIT_FAILURE_THRESHOLD: int = 5
    # 断路器开路后多久进入 HALF_OPEN 探活
    LLM_CIRCUIT_RECOVERY_TIMEOUT: float = 60.0


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
