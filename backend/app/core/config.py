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

    # ---- Phase 4 A5: Multi-Query 并行检索 ----
    # 启用后 search_multi_policy 用 ThreadPoolExecutor 并行调用 N 路 search_policy
    # 默认 true（性能优化：3 路串行 → 并行，~2.9x 加速）；关掉回退串行（debug 用）
    MULTI_QUERY_PARALLEL: bool = True
    # ThreadPoolExecutor max_workers（与 MULTI_QUERY_COUNT 默认对齐；per-request executor）
    MULTI_QUERY_WORKERS: int = 3

    # ---- Phase 4 A8: 融合后 rerank ----
    # True 时 search_multi_policy 走 N 路粗排 → RRF 融合 → 1×rerank → top-k
    #   - LLM rerank 调用次数：N → 1（默认 3 → 1，省 ~66% token）
    #   - rerank 视角：单路内部 → 全局融合候选（信息更全）
    # False 时回退 A5 路径：每路各自 rerank → RRF 融合（debug / A/B 对比用）
    MULTI_QUERY_FUSE_FIRST_RERANK: bool = True

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

    # ---- C2: Agent Function Calling 框架 ----
    # 启用后 orchestrator 把 FC-capable query 路由到 chat.agent_runner
    # （tools/registry.py 注册的 lookup_order / search_product / search_policy）
    # 默认 false（灰度用），关闭时走原 intent 分派路径（CLAUDE.md §9.2.2 隔离）
    ENABLE_AGENT_FC: bool = False
    # Agent 主循环最大工具轮次（防 LLM 死循环/成本失控）
    # 经验值 5：足够覆盖"查订单 → 查商品 → 查政策"三步场景
    MAX_AGENT_TURNS: int = 5

    # ---- M14: 用户上下文 + 订单 Resolver + 业务流 ----
    # ENABLE_CONTEXT_STORE: ContextService 是否读/写 conversation_contexts 表
    # 关闭时所有 ContextService 调用短路（不读不写），orchestrator 走老路径
    # 默认 false（灰度用）；ContextService.load 返空 context / update 返 True 短路
    ENABLE_CONTEXT_STORE: bool = False
    # ENABLE_ORDER_RESOLVER: OrderContextResolver 是否参与 _handle_order 决策
    # 关闭时 Resolver 返 DIRECT_ANSWER（orchestrator 走老路径 list_user_orders + LLM）
    # 默认 false（灰度用）；开 true 后 _handle_order 走 0/1/N 决策树
    ENABLE_ORDER_RESOLVER: bool = False
    # ENABLE_BUSINESS_FLOW: business_flow 工厂是否启用（M14 §10 阶段 3 占位）
    # 关闭时 refund_query / order_query 走原有分派路径，不走显式状态机
    # 默认 false（灰度用）
    ENABLE_BUSINESS_FLOW: bool = False
    # SSE_CARD_V2: 是否在 SSE meta 事件携带 OrderCard payload
    # 关闭时 orchestrator 不填 meta.card 字段（向后兼容老前端）
    # 默认 true（M14 阶段 2 落地的 SSE 协议扩展，前端可选订阅）
    SSE_CARD_V2: bool = True
    # ENABLE_ESCALATION_HANDOFF: 转人工兜底（M14 V3）
    # 开启后：Agent 异常 / 用户说"转人工" → 走 EscalationService，handoff payload 推到 SSE meta
    # 关闭时：所有 handoff 触发降级为"系统繁忙，请稍后再试"文本
    # 默认 false（灰度用）；demo 时可开 true
    ENABLE_ESCALATION_HANDOFF: bool = False

    # ---- P1-3: RAG ingest MySQL 失败回滚 Qdrant ----
    # 触发场景：upsert_points 成功 → upsert_knowledge_meta 失败 → 孤儿点残留
    # 开启后：自动 delete_points(chunk_ids) 回滚；rollback 失败仅 log warning（不掩盖 MySQL 错误）
    # 关闭则保留原行为：静默成功 + warn log（与 M14 V3 前一致）
    # 默认 True（数据一致性优先）；切 False 即可观察老行为用于 A/B 对比
    RAG_ROLLBACK_ON_MYSQL_FAIL: bool = True

    # ---- P1-1: RAG chunk_id 基于内容 hash 稳定 ----
    # 旧逻辑：point_id = uuid5(source + ":" + i)，基于下标
    #   问题：source 中增/删 chunk → 后续所有 ID 整体偏移 → 删除旧点找不到新 ID → 不幂等
    # 新逻辑：point_id = uuid5(source + ":" + chunk_hash[:32])，基于内容 sha256
    #   收益：同一 source 的同一段文本永远得到同一 ID → 重跑幂等、增量更新安全
    # 开启后：新数据用新 ID（与旧数据共存，不影响检索）
    # 关闭则回退旧逻辑（仅用于 A/B 对比）
    # 默认 True（数据稳定性优先）；旧点需 scripts/migrate_chunk_id.py 手动迁移
    RAG_CHUNK_ID_BY_CONTENT_HASH: bool = True

    # ---- P1-2: RAG BM25 索引后台异步重建 ----
    # 开启后 ingest 完成后触发后台线程重建 BM25 索引（不阻塞主流程）
    #   - 收益：避免首次 BM25 检索触发懒加载导致 1-3s RT spike
    #   - 关闭则保留懒加载（首次调用时构建，<100KB 数据集耗时 <1s）
    # 关闭则 BM25 索引只在首次 bm25_search 调用时懒构建（与原行为一致）
    # 默认 True（响应优先）；切 False 可观察懒加载行为（A/B 对比）
    RAG_BM25_EAGER_BUILD: bool = True

    # ---- P3-3: RRF 类型加权（按 doc_type 给最终 rrf_score 加权重）----
    # 业务策略：policy（政策）类文档加权 > faq > product（商品信息）
    #   - policy: 1.2（用户最关心"能不能退/怎么退"，policy 命中应优先）
    #   - faq: 1.0（中性，按 RRF 排名）
    #   - product: 0.9（次要，避免商品信息压制政策命中）
    # 关闭则空 dict，所有 doc 一视同仁（与原 RRF 行为一致）
    # 收益：政策类 query 召回 top-1 准确率 +10-20%（场景：用户问"运费险怎么买"）
    RAG_TYPE_BOOST: dict = {
        "policy": 1.2,
        "faq": 1.0,
        "product": 0.9,
    }

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
