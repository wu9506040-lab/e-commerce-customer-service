> **本文档已归档（2026-07-11）**。原 `docs/development/roadmap_v1.md` 基于理论架构设计；
> 现 `docs/development/roadmap.md` V2 基于实际代码扫描（10,290 行 Python）生成，**替换本文档**。
>
> V1 与 V2 的关键差异：
> - V1 是 Tech Lead 对照"理想架构"的设计草案（未对接实际代码）
> - V2 严格基于 2026-07-11 实际代码扫描：列出 12 项 V2.1 §9 合规缺口，给出 6 个 Sprint 修复路径
> - V1 与 V3.1 业务架构部分已并入 `docs/architecture/business.md`，本文档仅保留演进历史参考
>
> 历史文档代号：DP-V1 (Development Plan V1)

# 电商智能客服员工平台 — 当前版本分析与下一阶段开发计划（V1 · 已归档）

> 文档代号：DP-V1 (Development Plan V1)
> 文档版本：V0.1（初版，基于项目真实扫描）
> 文档状态：🗄️ 已归档（2026-07-11，由 `roadmap.md` V2 替换）
> 输入来源：
>   - 业务架构基线 `docs/business_architecture_v3.md` (BA-V3.1)
>   - 工程纪律 `CLAUDE.md §8`（架构设计要求 18 条）
>   - 项目实际扫描报告（2026-07-11，详见附录 A）
> 维护者：Tech Lead + 业务架构师
> 最近更新：2026-07-11

---

## 0. 文档元信息

| 项 | 值 |
|---|---|
| 起点 | V1.2（公网演示级，M13 完成） |
| 终点 | V2.0（接口抽象 + 单租户完善） → V3.0（多租户 SaaS） |
| 周期 | 12-18 个月（与 BA-V3.1 同步） |
| 范围 | 后端架构改造 / Prompt 独立化 / AI 能力抽象 / 多租户基线 |
| 不在范围 | 前端大改 / 知识库内容扩充 / 平台 Adapter 新增 |
| 不重写原则 | 9 个 ORM 模型 / 19 个 service 核心方法 / 115 个 pytest / 5 服务 docker-compose 全部保留 |

---

## 1. 当前项目状态分析

### 1.1 已完成能力（M1-M13）

按业务能力维度梳理，不按时间顺序：

| 能力域 | 完成项 | 关键文件 |
|--------|--------|----------|
| **数据层** | 9 张表 + ORM 2.0 + mock 种子 + soft delete | `models/*.py` |
| **鉴权** | JWT Cookie + bcrypt + RBAC（user/admin/visitor）| `services/auth_service.py` + `api/auth.py` |
| **意图分类** | 4 类意图（规则优先 + LLM 兜底）| `services/intent_service.py` |
| **RAG 检索** | 切片 + 向量化 + Qdrant + BM25 + RRF + Rerank + Query Rewriter | `services/rag/*` + `services/policy_service.py` |
| **Synthesizer** | 多源融合主入口 + 5 防 prompt 硬约束 + 来源标签 | `services/synthesizer.py` (750 行) |
| **退款流** | V2 旧版 + V3 LangGraph 6 Node 状态机（USE_LANGGRAPH_REFUND 开关）| `services/refund_graph.py` |
| **订单闭环** | 5 API（create/pay/ship/confirm/refund）+ 状态机 | `api/shop.py` + `services/order_lifecycle.py` |
| **InputGuard** | 3 层防御（L1 规则 / L2 centroid / L3 Redis）| `services/guard.py` + `guard_centroid.py` |
| **行为监控** | 5 类异常告警 | `services/behavior_monitor.py` |
| **响应缓存** | L1 exact + L2 semantic + max_tokens | `services/response_cache.py` |
| **健壮性** | 重试 + 抖动 + 断路器 + SSE heartbeat | `core/qwen.py` + `core/circuit_breaker.py` |
| **可观测性** | Request ID 全链路 + JSON 日志 + `/api/metrics` + `Metrics` 单例 | `core/logging.py` + `core/context.py` + `services/metrics.py` |
| **前端** | 6 页面 + 11 组件 + 自研"京东风"UI（无 UI 组件库）| `frontend/src/views/*` + `components/*` |
| **部署** | Docker Compose 5 服务 + prod override + 公网 ECS 部署 | `deploy/*` |
| **测试** | 115 pytest + ~100 E2E + 20 黑盒 | `backend/tests/` + `scripts/verify_*.py` |

### 1.2 当前架构现状

#### 1.2.1 后端结构

```
backend/app/
├── api/        10 个 router（chat / auth / admin / conversations / intent / public / shop / deps / middleware）
├── services/   19 个 service + services/rag/ 子包
├── core/       config / qwen / embedding / security / logging / context / circuit_breaker
├── clients/    qdrant / redis / mysql
├── models/     9 张表的 SQLAlchemy 2.0 ORM
├── schemas/    Pydantic Request/Response（5 模块）
├── tools/      OrderTool / ProductTool / RefundTool（静态方法集）
└── utils/      空
```

**关键事实：**

- 总计 ~60 个 .py 文件（不含 `__pycache__`）
- **0 个 Protocol / ABC / abstractmethod**（全仓库搜索仅命中测试类名 `TestSSEProtocol`）
- 所有"接口"都是**鸭子类型 + 静态方法集**（如 `Synthesizer.run_stream` 是 `@staticmethod`，`OrderTool` 是纯静态方法集合）
- 全部直接 `import` 具体类，无 DI 容器
- 入口组装仅在 `main.py` 完成

#### 1.2.2 数据库

| 表名 | 关键字段 | 缺什么 |
|------|----------|--------|
| users | role（RBAC）| tenant_id |
| conversations | session_id, user_id | tenant_id |
| messages | session_id, user_id, role, content, contexts(JSON), scores(JSON), token_count, latency_ms | tenant_id |
| products | sku, name, price, attributes(JSON), review_text, stock | tenant_id |
| orders | order_no, user_id, status(enum) | tenant_id |
| order_items | order_id, product_id, qty, unit_price | tenant_id |
| refunds | refund_no, order_id, user_id, status(enum), amount | tenant_id |
| knowledge_documents | source, doc_type, uploader_id | tenant_id |
| operation_logs | user_id, action, target_type, detail(JSON) | tenant_id |

**关键事实：**

- **全部 9 张表无 `tenant_id` 字段**（全局搜索 `tenant|租户|多租户` 零命中）
- 外键用 BigInteger 软关联，无 SQLAlchemy `relationship()`、无 `ForeignKey` 约束
- 枚举：`OrderStatus`（6 态）/ `RefundStatus`（4 态）

#### 1.2.3 API 路由

| 前缀 | 文件 | 端点数 | 版本控制 |
|------|------|--------|----------|
| `/api` | `chat.py` | 1（SSE 流式）| 无 |
| `/api/auth` | `auth.py` | 5 | 无 |
| `/api/admin` | `admin.py` | 5 | 无 |
| `/api/conversations` | `conversations.py` | 4 | 无 |
| `/api/intent` | `intent.py` | 1 | 无 |
| `/api/public` | `public.py` | 2 | 无 |
| `/api` | `shop.py` | 9（商品 2 + 订单 7）| 无 |
| `/api/metrics` | `main.py` | 1 | 无 |
| `/health` | `main.py` | 1 | 无 |

**关键事实：** URL 路径中**无版本号**（无 `/v1/` `/v2/`）。

### 1.3 已存在的问题

| # | 问题 | 严重度 | 来源 |
|---|------|--------|------|
| 1 | **接口驱动完全缺失**（0 Protocol/ABC）| 🔴 极高 | 扫描结果 §2 |
| 2 | **多租户完全缺失**（9 表无 tenant_id）| 🔴 极高 | 扫描结果 §5 |
| 3 | **AI 能力无抽象**（LLM/Embedding 直接 import）| 🔴 极高 | 扫描结果 §7 |
| 4 | **Prompt 全部硬编码**（7 处模板散落）| 🟠 高 | 扫描结果 §7 |
| 5 | **synthesizer.py 750 行单一文件**，多职责混杂 | 🟠 高 | 代码规模 |
| 6 | **事件驱动缺失**，跨模块通知全靠直接调用 | 🟠 高 | 扫描结果 §8 |
| 7 | **配置与代码混杂**（阈值/规则硬编码）| 🟠 高 | 扫描结果 §6 |
| 8 | **限流单进程内存版**（多实例部署失效）| 🟡 中 | `api/middleware.py:145` |
| 9 | **可观测性不足**（无 Prometheus/OpenTelemetry）| 🟡 中 | 扫描结果 §6 |
| 10 | **无 CI/CD**（手工部署）| 🟡 中 | 扫描结果 §5 |

### 1.4 技术债清单（12 条）

来自项目扫描报告 §12，按"改动量 × 风险"排序：

| # | 技术债 | 文件:行 | 风险 | 紧迫度 |
|---|--------|---------|------|--------|
| 1 | `order_service.get_order_detail` 因 OrderTool 不暴露 id 导致 N+1 | `services/order_service.py:57-58` | 中 | 中 |
| 2 | `shop.py list_my_orders` 每行重查 OrderItem | `api/shop.py:118-132` | 中 | 中 |
| 3 | V2 refund 路径 deprecated 仍在代码（开关默认 False 但代码保留）| `services/synthesizer.py:569-573` | 低 | 低 |
| 4 | V1 pipeline.run 几乎不调用，仅 synthesizer 异常 fallback | `services/rag/pipeline.py` | 极低 | 极低 |
| 5 | Qdrant `vectors_count` 字段在 1.12.x 为 None | `clients/qdrant.py:247` | 低 | 低 |
| 6 | Pipeline.PROMPT_TEMPLATE + synthesizer.SYSTEM_PROMPT_BASE 两套并存 | `services/rag/pipeline.py:32` + `services/synthesizer.py:48` | 中 | 中 |
| 7 | 限流单进程内存版（多实例失效）| `api/middleware.py:145` | 中 | 中 |
| 8 | JWT SECRET 强校验仅 prod 环境 | `core/config.py:_validate_jwt_secret()` | 低 | 低 |
| 9 | 无 tenant_id | 9 张表 | 高 | 高 |
| 10 | synthesizer.run_stream 同步包装异步（SSE 主循环 to_thread）| `api/chat.py:260-265` | 低 | 低 |
| 11 | shop.py 5 个 endpoint 无 audit 上报 | `api/shop.py:201-289` | 低 | 低 |
| 12 | shop.py Query 同名导入 | `api/shop.py:108` | 极低 | 极低 |

### 1.5 可复用部分（不重写基线）

| 类别 | 项目 | 复用方式 |
|------|------|----------|
| **数据层** | 9 个 ORM 模型 + 软删 + 索引 | **保留全部**，仅补 tenant_id |
| **业务层** | 19 个 service 核心方法 | **保留核心**，仅在依赖边界加 Protocol |
| **业务层** | OrderTool/ProductTool/RefundTool | **保留**，补回 id 字段 |
| **API 层** | 10 个 router 的 endpoint 形状 | **保留**，仅在入口组装时换实现 |
| **核心层** | qwen.py / embedding.py / circuit_breaker.py | **保留函数签名**，外面包一层 Protocol |
| **RAG** | rag 子包全部（pipeline/ingest/knowledge）| **保留** |
| **退款流** | refund_graph.py（LangGraph 6 Node）| **保留**，删除 V2 双轨态 |
| **前端** | 6 页面 + 11 组件 | **保留全部** |
| **部署** | docker-compose 5 服务 | **保留**，仅加 AI Provider 配置项 |
| **测试** | 115 pytest + ~100 E2E | **保留全部**，新模块补充测试 |
| **种子数据** | 10 商品 + 7 订单 | **保留** |
| **监控** | /health + /api/metrics + Request ID | **保留**，扩展指标维度 |

---

## 2. 架构符合度评估（对照 CLAUDE.md §8）

### 2.1 评估总览

| 原则 | §8 对应小节 | 现状 | 评级 | 关键差距 |
|------|------------|------|------|----------|
| **模块化（高内聚低耦合）** | 8.2 | 19 service 但 synthesizer 750 行混杂多职责 | 🟠 部分达标 | 单文件多职责 |
| **接口驱动** | 8.1 + 8.3 | **0 个 Protocol/ABC**，全部直接 import | 🔴 完全不达标 | 切实现要改所有调用点 |
| **分层架构** | 8.2.3 | api → services → clients 整体符合 | 🟢 基本达标 | 局部 service 内部混乱 |
| **AI 能力抽象** | 8.3.3 | LLM/Embedding/Rerank 直接 import 具名函数 | 🔴 完全不达标 | 切 LLM provider 要改 N 处 |
| **数据隔离** | 8.4.1 + 8.4.3 | 9 表无 tenant_id，无外键约束 | 🔴 完全不达标 | 多租户要全量重构 |

**总评：3 项 🔴 + 1 项 🟠 + 1 项 🟢。3 项 🔴 是企业级交付的核心阻塞。**

### 2.2 模块化评估

| 检查项 | 现状 | 评估 |
|--------|------|------|
| 模块职责清晰度 | 19 service 命名清晰（如 `OrderService`/`RefundService`），但 `synthesizer.py` 一个文件包含 7 个 `_handle_*` 私有方法 + 5 个 prompt 模板 + 工具调用 + 缓存键生成 | 🟠 |
| 是否存在业务混杂 | `synthesizer.py` 同时包含 Agent 决策、Prompt 模板、Tool 调用、缓存键生成、RAG 调用 — 违反 §8.2.1"高内聚低耦合" | 🔴 |
| 万能模块 | `chat.py`（SSE 编排）+ `synthesizer.py`（多职责）| 🟠 |
| 模块独立演进 | service 之间强耦合（如 `synthesizer` 直接调用 6 个其他 service）| 🟠 |

### 2.3 接口驱动评估

| 检查项 | 现状 | 评估 |
|--------|------|------|
| Protocol/ABC 数量 | **0**（全仓库搜索零命中）| 🔴 |
| 跨模块调用方式 | 全部 `from app.xxx import YyyClass; yyy_class.method()` | 🔴 |
| 是否有依赖注入 | 无 DI 容器，依赖组装仅在 `main.py` | 🔴 |
| 单元测试 mock 难度 | 高（无法注入 mock，必须真实连接）| 🟠 |

**影响：** 切换 LLM Provider（Qwen → GPT）需要改所有 `from app.core.qwen import chat` 的调用点，约 15+ 处。无法独立测试 RAG 链路。

### 2.4 分层架构评估

| 层 | 职责 | 现状 | 评估 |
|----|------|------|------|
| **API 层** | 路由 / 参数解析 / 调 services | 10 个 router 都只调 service，无业务逻辑 | 🟢 达标 |
| **业务服务层** | 业务编排（调 core/rag/clients）| service 之间大量相互调用（如 `synthesizer` 调 6 个 service）| 🟡 |
| **领域模块层** | 独立业务领域 | 缺（services/rag/ 是子包但不是领域）| 🟠 |
| **基础设施层** | Qdrant / Redis / MySQL 连接 | clients/ 三个文件隔离清晰 | 🟢 达标 |

**关键问题：** 没有独立的"领域模块层"，业务逻辑直接铺在 services/ 下。

### 2.5 AI 能力抽象评估

| AI 能力 | 当前实现 | 抽象状态 | §8.3.3 合规 |
|---------|----------|----------|-----------|
| **LLM** | `core/qwen.py` 单例 OpenAI 客户端 + 函数 `chat()`/`stream_chat()` | 无 Protocol | 🔴 |
| **Embedding** | `core/embedding.py` `embed_text/embed_texts/embed_text_or_mock` | 无 Protocol | 🔴 |
| **Rerank** | `services/rerank.py` `rerank/rerank_async` | 无 Protocol | 🔴 |
| **Speech** | 未实现 | — | — |
| **Prompt** | 7 处硬编码在业务代码 | 散落 | 🔴（违反 §8.6）|

**切换成本估算：**
- 换 LLM Provider：改 15+ 个 `from app.core.qwen import chat` 调用点
- 换 Embedding：改 3 个 `embed_texts` 调用点 + 1 个 RAG pipeline
- Prompt 改一处：需 grep 找到所有使用点 → 风险高

### 2.6 数据隔离评估

| 检查项 | 现状 | 评估 |
|--------|------|------|
| tenant_id 字段 | 9 表全无 | 🔴 |
| 外键约束 | 软关联（BigInteger + 注释）| 🟡 |
| 数据访问隔离 | 无（所有 service 共用一个 DB）| 🔴 |
| 查询过滤 | 无 tenant 过滤层 | 🔴 |

**影响：** 多租户 SaaS 化需要：
1. 9 张表全部加 tenant_id 字段 + 索引
2. 所有 query 加 tenant_id 过滤
3. 认证层解析 tenant
4. 数据迁移脚本（从无 tenant → 有 tenant）

**预计工作量：** 3-4 周（纯数据层），加 service 改造共 6-8 周。

---

## 3. 下一阶段开发路线（P0/P1/P2）

### 3.1 P0：必须解决（影响扩展性 / 稳定性 / 后续开发）

| # | 任务 | 工作量 | 阻塞什么 |
|---|------|--------|----------|
| P0-1 | **接口抽象基线**：抽出 `LLMProvider` / `EmbeddingProvider` / `VectorStore` / `PolicyService` 4 个核心 Protocol | 2 周 | P0-3、P1-1、P1-2 |
| P0-2 | **tenant_id 全量补齐**：9 张表加字段 + 所有 query 加过滤 + 数据迁移脚本 | 3 周 | P2-1 |
| P0-3 | **AI Provider 抽象**（基于 P0-1）：QwenProvider / DashScopeEmbeddingProvider 实现 + 切换开关 | 2 周 | 多 LLM 路由 |
| P0-4 | **Prompt 模板独立化**：7 处硬编码 → `config/prompts/*.yaml` + DB 表 + 版本管理 | 2 周 | 企业定制 |
| P0-5 | **业务规则配置化**：转人工阈值 / 情绪阈值 / Persona / 安全规则 → 配置中心 | 1 周 | 多租户定制 |
| P0-6 | **修复 OrderTool N+1**：暴露 id 字段 + 改 OrderService.get_order_detail + 改 shop.py list_my_orders | 0.5 周 | 性能 |
| P0-7 | **修复 shop.py audit 缺失**：5 个订单状态流转 endpoint 加 `try_log_action` | 0.5 周 | 合规 |

**P0 合计：~11 周（约 3 个月）**

### 3.2 P1：提升产品能力

| # | 任务 | 工作量 | 价值 |
|---|------|--------|------|
| P1-1 | **事件总线引入**：建立 `EventBus` Protocol + Redis Pub/Sub 实现 | 2 周 | 模块解耦 |
| P1-2 | **5 防 AI 安全控制层独立**：从 synthesizer 抽出 `SafetyChecker` Protocol + 5 个检查器 | 2 周 | 安全审计 |
| P1-3 | **限流改 Redis 版**：替换 `api/middleware.py` 单进程内存版 | 1 周 | 多实例部署 |
| P1-4 | **可观测性增强**：结构化日志字段补全 + Prometheus exporter（`/metrics` 兼容格式）| 2 周 | 运维 |
| P1-5 | **CI/CD 基础**：GitHub Actions（lint + pytest + docker build）| 1 周 | 自动化 |
| P1-6 | **Synthesizer 拆分**：750 行单文件拆为 `PolicySynthesizer` / `RefundSynthesizer` / `ProductSynthesizer` / `OrderSynthesizer` | 3 周 | 模块化 |

**P1 合计：~11 周（约 3 个月）**

### 3.3 P2：商业化与长期演进

| # | 任务 | 工作量 | 触发条件 |
|---|------|--------|----------|
| P2-1 | **多租户隔离策略升级**：从"共享 DB + tenant_id 过滤"升级到"Schema 隔离"或"DB 实例隔离" | 4 周 | 有大客户 |
| P2-2 | **V2 refund 双轨态清理**：删除 `USE_LANGGRAPH_REFUND` 开关和 V2 路径（V3 稳定后）| 0.5 周 | V3 灰度完成 |
| P2-3 | **Prompt 版本管理 + 灰度**：DB 表 + 流量比例控制 | 2 周 | 企业定制需求 |
| P2-4 | **企业定制层**：租户级 Persona / Prompt / 规则覆盖 | 3 周 | 多客户 |
| P2-5 | **SaaS 化部署自动化**：Terraform / Helm / 一键开租户 | 4 周 | 商业化 |
| P2-6 | **管理后台（Web）**：KB CRUD / 工单 / 数据看板 / 多租户管理 | 6 周 | BA-V3.1 §6 |
| P2-7 | **运营 Agent + 数据中台 V1**：M17/M18 | 8 周 | BA-V3.1 §12 |

**P2 合计：~27.5 周（约 7 个月），分阶段触发**

### 3.4 路线图总览

```
M14（8-10 周）    P0-1 ~ P0-7     接口抽象 + tenant_id + AI Provider + Prompt 独立 + 配置化
M15（4-6 周）     P1-1 ~ P1-3     事件总线 + 5 防 + Redis 限流
M16（6-8 周）     P1-4 ~ P1-6     可观测性 + CI/CD + Synthesizer 拆分
M17（8 周）       P2-1, P2-2      多租户升级 + V2 cleanup
M18（6-8 周）     P2-3 ~ P2-7     Prompt 灰度 + 企业定制 + SaaS 化 + 管理后台 + Agent
```

---

## 4. Sprint 迭代计划

每个 Sprint **2-3 周**，**基于现有代码演进**，**不重写**。

### Sprint 1（P0-1 · 2 周）— 接口抽象基线

| 项 | 内容 |
|----|------|
| **目标** | 抽出 4 个核心 Protocol，service 依赖边界清晰化 |
| **任务** | 1. 定义 `LLMProvider` Protocol（`chat`/`stream_chat`/`get_model_name`）<br>2. 定义 `EmbeddingProvider` Protocol（`embed_text`/`embed_texts`）<br>3. 定义 `VectorStore` Protocol（`search`/`upsert`/`delete`）<br>4. 定义 `PolicyService` Protocol（`search_policy`）<br>5. 重命名现有 `qwen.py` → `qwen_provider.py` 实现 `LLMProvider`<br>6. 重命名现有 `embedding.py` → `dashscope_embedding_provider.py` 实现 `EmbeddingProvider`<br>7. 重命名现有 `clients/qdrant.py` → `qdrant_vector_store.py` 实现 `VectorStore`<br>8. **零调用点改动**（Protocol 接口签名与现有函数签名一致）|
| **涉及模块** | `core/qwen.py`、`core/embedding.py`、`clients/qdrant.py`、`services/policy_service.py` |
| **验收** | 115 pytest 全通过 + 新增 4 个 Protocol 定义测试 + 服务正常启动 + 公网 demo 链路通畅 |
| **风险** | 低（接口签名兼容） |

### Sprint 2（P0-3 · 2 周）— AI Provider 抽象 + 切换

| 项 | 内容 |
|----|------|
| **目标** | 业务层通过 Protocol 调 LLM/Embedding，可运行时切换 Provider |
| **任务** | 1. `app/core/llm/__init__.py` 暴露 `get_default_llm_provider() -> LLMProvider`<br>2. `app/core/embedding/__init__.py` 暴露 `get_default_embedding_provider() -> EmbeddingProvider`<br>3. 全部 15+ 处 `from app.core.qwen import chat` 改为 `from app.core.llm import get_default_llm_provider; llm = get_default_llm_provider(); await llm.chat(...)`<br>4. 配置项 `LLM_PROVIDER=qwen` / `EMBEDDING_PROVIDER=dashscope` 支持切换<br>5. 加 mock provider 用于测试 |
| **涉及模块** | `core/llm/`、`core/embedding/`、所有 service 调用点 |
| **验收** | 115 pytest 全通过 + 新增 mock provider 测试 + 切换 provider 不需改代码 |
| **风险** | 中（涉及 15+ 调用点，需逐个验证） |

### Sprint 3（P0-2 · 3 周）— tenant_id 全量补齐

| 项 | 内容 |
|----|------|
| **目标** | 所有核心数据带 tenant_id，为多租户基线铺路 |
| **任务** | 1. 9 张表全部加 `tenant_id` 字段（默认 `tenant_id=1`）+ 索引<br>2. 数据迁移 SQL（`deploy/mysql/init/migrations/001_add_tenant_id.sql`）<br>3. 所有 query 加 `filter_by(tenant_id=...)` 或 SQLAlchemy event 监听自动注入<br>4. `User` 表加 `tenant_id` 字段（用户属于租户）<br>5. JWT 注入 `tenant_id` claim<br>6. `get_current_user` Depends 返回 `tenant_id`<br>7. 加 9 个迁移后 pytest |
| **涉及模块** | `models/*.py`、`core/security.py`、`api/deps.py`、所有 service |
| **验收** | 115 pytest 全通过 + 9 表 schema 含 tenant_id + 迁移脚本可回滚 |
| **风险** | 高（涉及所有数据访问） |

### Sprint 4（P0-4 · 2 周）— Prompt 模板独立化

| 项 | 内容 |
|----|------|
| **目标** | 7 处硬编码 Prompt → `config/prompts/*.yaml` + DB 表 |
| **任务** | 1. 建 `prompt_templates` 表（id, name, version, content, tenant_id, is_active）<br>2. 7 处硬编码 Prompt 移到 `config/prompts/{name}/v{n}.yaml`<br>3. `PromptRegistry` 服务：按 (name, tenant_id) 加载 + 缓存<br>4. `synthesizer._build_chat_prompt` 等改为 `prompt_registry.get("synthesizer.system", tenant_id)`<br>5. 后台 API：`GET/PUT /api/admin/prompts`（admin 权限）<br>6. 加 9 个 Prompt 测试 |
| **涉及模块** | `services/synthesizer.py`、`services/rag/pipeline.py`、`services/rerank.py`、`services/query_rewriter.py`、`services/refund_graph.py`、`services/intent_service.py` |
| **验收** | 115 pytest 全通过 + 7 个 Prompt 移到文件 + 管理后台可改 Prompt（重启生效） |
| **风险** | 中（影响所有 LLM 调用质量） |

### Sprint 5（P0-5 + P1-1 · 3 周）— 业务规则配置化 + 事件总线

| 项 | 内容 |
|----|------|
| **目标** | 阈值/规则可配置 + 跨模块通知走事件 |
| **任务** | 1. 建 `system_configs` 表（key, value, tenant_id, type）<br>2. `ConfigService` 服务：CRUD + 缓存<br>3. 业务规则改为 `config_service.get("emotion_transfer_threshold", tenant_id, default=80)`<br>4. `EventBus` Protocol + Redis Pub/Sub 实现<br>5. `Conversation` 模块改产生事件（`HighRiskConversationDetected`）<br>6. `Emotion` / `CustomerService` / `Analytics` 订阅事件<br>7. 加 12 个测试 |
| **涉及模块** | `services/config_service.py`、`core/events/`、`services/emotion/`、`services/conversation/` |
| **验收** | 业务规则改配置无需重启 + 事件驱动链路正常 |
| **风险** | 中（涉及业务规则读取） |

### Sprint 6（P0-6 + P0-7 + P1-3 · 2 周）— 修技术债 + 限流升级

| 项 | 内容 |
|----|------|
| **目标** | 修复 N+1、补 audit、限流改 Redis |
| **任务** | 1. OrderTool 暴露 `id` 字段 + OrderService 去除重查<br>2. shop.py 5 个订单流转 endpoint 加 `try_log_action`<br>3. `api/middleware.py:145` 限流改 Redis Lua 脚本<br>4. 加 8 个测试 |
| **涉及模块** | `tools/order_tool.py`、`services/order_service.py`、`api/shop.py`、`api/middleware.py` |
| **验收** | N+1 修复 + audit 100% 覆盖 + 多实例部署限流生效 |
| **风险** | 低（已知技术债） |

### Sprint 7（P1-2 + P1-6 · 3 周）— 5 防安全层独立 + Synthesizer 拆分

| 项 | 内容 |
|----|------|
| **目标** | 安全控制可独立演进 + Synthesizer 模块化 |
| **任务** | 1. `SafetyChecker` Protocol + 5 个检查器实现（防幻觉/承诺/越权/敏感/情绪升级）<br>2. `SafetyPipeline` 编排 5 防<br>3. `synthesizer.py` 750 行拆为 4 个 `*Synthesizer` 子模块<br>4. 加 15 个测试 |
| **涉及模块** | `services/safety/`、`services/synthesizer/`（拆分为子包）|
| **验收** | 5 防独立可测 + Synthesizer 单文件 ≤300 行 + 公网 demo 行为不变 |
| **风险** | 中（Synthesizer 是核心入口） |

### Sprint 8（P1-4 + P1-5 · 2 周）— 可观测性 + CI/CD

| 项 | 内容 |
|----|------|
| **目标** | 运维可见 + 自动化构建 |
| **任务** | 1. `/api/metrics` 输出 Prometheus 格式<br>2. 加 5 个新指标（AI 自助率 / 错误率 / 5 防触发率）<br>3. GitHub Actions：lint + pytest + docker build<br>4. 加 docker-compose healthcheck.io 探针 |
| **涉及模块** | `services/metrics.py`、`api/metrics.py`、`.github/workflows/` |
| **验收** | Prometheus 可抓取 + CI 全绿 + healthcheck.io 接入 |
| **风险** | 低 |

### Sprint 9-12（P2 触发式 · 后续）— 多租户升级 + V2 cleanup + 商业化

按需触发，详见 §3.3。

---

## 5. 重构策略

### 5.1 重构决策矩阵

| 类别 | 项目 | 收益 | 风险 | 投入 | 触发 |
|------|------|------|------|------|------|
| **保留** | 9 个 ORM 模型 | 100% 复用 | 0 | 0 | — |
| **保留** | 19 个 service 核心方法 | 100% 复用 | 0 | 0 | — |
| **保留** | 115 pytest | 100% 复用 | 0 | 0 | — |
| **保留** | 5 服务 docker-compose | 100% 复用 | 0 | 0 | — |
| **保留** | 前端 6 页面 + 11 组件 | 100% 复用 | 0 | 0 | — |
| **小改** | OrderTool 暴露 id | 修 N+1 | 极低 | 0.5d | Sprint 6 |
| **小改** | shop.py 加 audit | 合规 | 极低 | 0.5d | Sprint 6 |
| **小改** | JWT 强校验扩 env | 安全 | 低 | 0.5d | Sprint 8 顺带 |
| **小改** | 删除 deprecated V2 refund 路径 | 代码清理 | 低 | 1d | Sprint 9（V3 稳定后）|
| **小改** | 删除 V1 pipeline.run | 代码清理 | 极低 | 0.5d | Sprint 9 顺带 |
| **小改** | Query 同名导入清理 | 代码质量 | 极低 | 0.5d | Sprint 8 顺带 |
| **重构** | 抽 4 个核心 Protocol | 接口驱动基线 | 中 | 2w | Sprint 1 |
| **重构** | AI Provider 抽象 + 切换 | 切模型成本下降 10x | 中 | 2w | Sprint 2 |
| **重构** | tenant_id 全量补齐 | 多租户基线 | 高 | 3w | Sprint 3 |
| **重构** | Prompt 模板独立化 | 企业定制能力 | 中 | 2w | Sprint 4 |
| **重构** | 业务规则配置化 | 零代码改规则 | 中 | 1w | Sprint 5 |
| **重构** | 事件总线引入 | 模块解耦 | 中 | 2w | Sprint 5 |
| **重构** | 5 防独立 + Synthesizer 拆分 | 模块化 + 安全审计 | 中 | 3w | Sprint 7 |
| **重构** | 限流改 Redis | 多实例部署 | 低 | 1w | Sprint 6 |
| **重构** | /metrics 兼容 Prometheus | 运维可见 | 低 | 1w | Sprint 8 |
| **重构** | CI/CD | 自动化 | 低 | 1w | Sprint 8 |
| **暂缓** | 多租户策略升级（Schema/DB 隔离）| 大客户支持 | 极高 | 4w | P2-1 触发 |
| **暂缓** | Synthesizer 异步化（原生 async）| 性能 | 中 | 2w | 暂缓到 V3.0 后 |
| **暂缓** | PROJECT_DESIGN.md 完善 | 文档 | 0 | 0.5d | 后续专项 |
| **暂缓** | SaaS 化部署自动化 | 商业化 | 高 | 4w | P2-5 触发 |

### 5.2 重构收益与风险分析

#### 重构收益（按价值排序）

| 重构 | 短期收益 | 长期收益 | 量化 |
|------|----------|----------|------|
| 抽 Protocol | 0 | 切实现成本从 15+ 处 → 1 处 | ↓ 93% 改动量 |
| AI Provider 抽象 | 0 | 多 LLM 路由 / 容灾 / A/B 测试 | 切模型 2 天 → 0.5 天 |
| tenant_id 补齐 | 数据可识别租户 | 多租户基线 / 数据隔离 | 避免后期 6-8 周重构 |
| Prompt 独立化 | 改 Prompt 不需发版 | 企业定制 / 灰度 / 热更新 | 改 Prompt 1 天 → 5 分钟 |
| 业务规则配置化 | 改规则不需发版 | 多租户定制 | 改规则 1 天 → 5 分钟 |
| 事件总线 | 解耦 | 新增模块 0 改原模块 | 新增分析模块 -80% 改动 |
| 5 防独立 | 可单测 | 安全审计 / 灰度 / 配置化 | 5 防单测覆盖率 0 → 80% |
| Synthesizer 拆分 | 可维护性 | 单文件职责清晰 | 750 行 → 4 个 ≤300 行 |
| 限流 Redis | 多实例支持 | 横向扩展 | 避免多实例失效 |
| /metrics Prometheus | 运维可见 | 告警 / 趋势分析 | 0 → 完整 |
| CI/CD | 自动化 | 质量保障 | 手工 → 自动化 |

#### 重构风险（按严重度排序）

| 重构 | 风险 | 缓解措施 |
|------|------|----------|
| tenant_id 全量补齐 | 高（影响所有数据访问）| 1. 用 SQLAlchemy event 监听自动注入<br>2. 加全量回归测试<br>3. 数据迁移脚本可回滚<br>4. 灰度发布 |
| Synthesizer 拆分 | 中（核心入口）| 1. 行为不变测试覆盖<br>2. 灰度对比 V2/V3 路径<br>3. 失败回滚 |
| AI Provider 抽象 | 中（15+ 调用点）| 1. 接口签名兼容<br>2. 115 pytest 全通过<br>3. 切换前先跑一周 mock provider |
| Prompt 独立化 | 中（影响所有 LLM）| 1. Prompt 内容不变迁移<br>2. A/B 测试新旧路径<br>3. 回滚开关 |
| 事件总线 | 中（跨模块链路）| 1. Redis Pub/Sub 监控<br>2. 失败重试<br>3. 直接调用兜底 |
| 5 防独立 | 中（顺序敏感）| 1. 单元测试覆盖每个防<br>2. 集成测试验证顺序<br>3. 失败回滚 |

### 5.3 重构纪律（CLAUDE.md §8 强约束）

| # | 纪律 | 来源 |
|---|------|------|
| 1 | 重构前必过 §8.7 自检 5 问 | CLAUDE.md |
| 2 | 重构后必更新 §8.8 模块交付 8 件套 | CLAUDE.md |
| 3 | 接口变更必同步更新所有调用方 | CLAUDE.md §8.7 #4 |
| 4 | 重构 commit 必带 `refactor:` 前缀 | git 规范 |
| 5 | 每个 Sprint 结束跑全量 115 pytest | 测试纪律 |

---

## 6. 目标架构与演进路径

### 6.1 演进路径

```
V1.2（当前）   单租户单体演示级
    ↓
V2.0（M14-M16）接口抽象 + AI Provider + tenant_id 基线 + Prompt 独立 + 事件总线 + CI/CD
    ↓
V3.0（M17-M18）多租户 SaaS + 管理后台 + 数据中台 V1 + 运营 Agent
    ↓
V4.0（未来）   多 Agent 编排 + 私有化部署 + 企业定制层 + AIGC 集成
```

### 6.2 V2.0 终态架构（目标）

```
┌─────────────────────────────────────────────────────┐
│                  API 层 (10 router)                  │
│  chat / auth / admin / conversations / intent /     │
│  public / shop / deps / middleware / metrics        │
└────────────────────────┬────────────────────────────┘
                         ↓ 调 service
┌─────────────────────────────────────────────────────┐
│              Application Service 层                   │
│  ChatService / OrderService / RefundService /       │
│  AuthService / ConfigService / PromptRegistry       │
│  SafetyPipeline / EventBus                          │
└────────────────────────┬────────────────────────────┘
                         ↓ 调 Protocol
┌─────────────────────────────────────────────────────┐
│                  Domain 层（新增）                    │
│  Synthesizer 子包（PolicySynthesizer / Refund /    │
│  Product / Order）                                   │
│  Conversation / Emotion / BehaviorMonitor           │
│  RAG（pipeline / ingest / knowledge）               │
└────────────────────────┬────────────────────────────┘
                         ↓ 调 Provider/Store Protocol
┌─────────────────────────────────────────────────────┐
│            Infrastructure 层（含 Protocol）          │
│  LLMProvider → QwenProvider（可换 GPTProvider）    │
│  EmbeddingProvider → DashScopeProvider             │
│  RerankProvider → QwenRerankProvider               │
│  VectorStore → QdrantStore（可换 MilvusStore）    │
│  EventBus → RedisPubSub                            │
│  Database → MySQL（带 tenant_id）                   │
│  Cache → Redis                                      │
└─────────────────────────────────────────────────────┘
```

**关键变化（vs V1.2）：**

| 维度 | V1.2 | V2.0 |
|------|------|------|
| 服务编排 | 直接 import 具体类 | 通过 Protocol + DI |
| LLM 调用 | `from app.core.qwen import chat` | `llm_provider = get_default_llm_provider(); await llm_provider.chat(...)` |
| 多租户 | 单租户 | 共享 DB + tenant_id 过滤 |
| Prompt | 7 处硬编码 | `config/prompts/*.yaml` + DB 版本管理 |
| 业务规则 | 硬编码阈值 | `ConfigService.get(...)` |
| 跨模块通知 | 直接调用 | EventBus（Redis Pub/Sub）|
| 5 防安全 | 散在 synthesizer | 独立 `SafetyPipeline` |
| Synthesizer | 750 行单文件 | 拆为 4 个子模块 |
| 限流 | 单进程内存 | Redis Lua |
| 监控 | 自研 JSON | Prometheus 兼容 |
| CI/CD | 手工 | GitHub Actions |

### 6.3 V3.0 终态（BA-V3.1 M17-M18 目标）

| 维度 | V3.0 目标 |
|------|-----------|
| 多租户 | Schema 隔离（按客户规模升级）|
| 管理后台 | Web 后台（KB / 工单 / 数据看板 / 租户管理）|
| 数据中台 | ODS/DWD/DWS/ADS + Airflow 调度 |
| 平台 Adapter | 淘宝 → 京东 → 拼多多（按 BA-V3.1 §12）|
| 运营 Agent | 商品图 / 脚本生成（按 BA-V3.1 §3.5）|
| 业务 KPI | AI 自助率 ≥85% / 首响 ≤8s / CSAT ≥4.5 |

### 6.4 终态验收标准

| 维度 | 标准 |
|------|------|
| **业务** | BA-V3.1 §15 KPI（M14: 自助率 ≥60% / 首响 ≤15s / 错误率 ≤3%）|
| **架构** | CLAUDE.md §8 5 大原则全部达标 |
| **工程** | 0 个硬编码 Prompt / 0 个硬编码阈值 / 9 表全有 tenant_id |
| **测试** | 单元测试 ≥150 条 / 覆盖率 ≥70% / E2E ≥150 条 |
| **部署** | CI/CD 全绿 / 灰度发布能力 / 健康检查 100% 覆盖 |
| **可观测** | Prometheus 抓取 / Grafana 看板 / 告警规则 |

---

## 附录 A：信息收集盲区（待用户补充）

以下信息缺失会影响决策，需要补充：

| # | 待澄清 | 影响 |
|---|--------|------|
| Q1 | **团队规模**（当前工程师数量 + 是否招聘）| Sprint 节奏 / M14-M18 排期 |
| Q2 | **是否已有付费客户 / POC 客户**| P2 触发时机 |
| Q3 | **多租户策略优先级**（共享 DB vs Schema 隔离）| Sprint 3 实施细节 |
| Q4 | **现有 7 笔订单种子是否需要按租户维度隔离**| 数据迁移脚本 |
| Q5 | **是否需要保留旧 V2 refund 路径作为回滚兜底**| Sprint 9 清理时机 |
| Q6 | **公网 demo 是否会因重构中断**| Sprint 1-2 的灰度策略 |
| Q7 | **健康检查 UUID**（healthcheck.io 接入用）| Sprint 8 |
| Q8 | **GitHub Actions 是否需要 secret 管理**| Sprint 8 CI 配置 |
| Q9 | **是否需要兼容 Python 3.11 / 3.12 双版本**| CI matrix |

---

## 附录 B：变更记录

| 版本 | 日期 | 作者 | 变更 |
|------|------|------|------|
| V0.1 | 2026-07-11 | Tech Lead + 业务架构师 | 初版，基于项目真实扫描报告 |

---

## 附录 C：引用文档

| 文档 | 用途 |
|------|------|
| `docs/business_architecture_v3.md` (BA-V3.1) | 业务架构基线（产品视角） |
| `CLAUDE.md §8` | 工程架构纪律（18 条架构设计要求） |
| `docs/learning_log.md` | M1-M13 演进记录 |
| `docs/PROJECT_DESIGN.md` | 项目总设计（草稿） |
| `docs/OPERATIONS.md` | 运维指南 |
| `docs/HEALTHCHECK.md` | healthcheck.io 接入指南 |
| `docs/demo_walkthrough_report.md` | M13 公网演示报告 |