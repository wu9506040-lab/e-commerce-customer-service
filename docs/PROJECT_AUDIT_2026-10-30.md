# 项目现状审计报告 · 全量评估与修复路线

> **版本**：v1.0 · 2026-10-30
> **范围**：`backend/app` + `frontend/src` + `deploy/` + `docs/` 全量扫描 + 实测验证
> **基线文件**：`AGENTS.md`（V2.1）/ `CLAUDE.md`（V2.1，镜像）/ `docs/architecture/system.md` / `docs/development/roadmap.md`
> **方法**：静态扫描（目录/引用/规则命中）+ 动态验证（pytest / coverage / vite build / 密钥扫描）
> **结论一句话**：工程底子扎实，**真实短板不在代码，而在治理基线已落后代码一个版本**——Milvus 禁令、状态文档、租户字段三处同源。

---

## 0. 目录

| 章节 | 内容 |
|------|------|
| [1](#1-执行摘要) | 执行摘要（一页纸） |
| [2](#2-审计方法与可复现命令) | 审计方法与可复现命令 |
| [3](#3-规模实测) | 规模实测 |
| [4](#4-质量基线实测) | 质量基线实测 |
| [5](#5-架构现状评估) | 架构现状评估 |
| [6](#6-合规对标表) | 合规对标表（逐条 AGENTS.md） |
| [7](#7-风险登记册) | 风险登记册（P0/P1/P2） |
| [8](#8-修复路线图) | 修复路线图（三批次） |
| [9](#9-回归与验证计划) | 回归与验证计划 |
| [附录 A](#附录-a审计命令清单) | 审计命令清单 |
| [附录 B](#附录-b证据索引) | 证据索引（file:line） |

---

## 1. 执行摘要

### 1.1 总体判断

项目已达到**可交付的企业级中台形态**，非 Demo：

- 分层清晰、接口抽象就近落地、业务规则全面配置化
- 测试量与代码量比 ≈ 0.60（20,643 / 34,519），1216 用例全绿，覆盖率 65%
- 具备罕见的**自我批判工程文化**（`docs/OPTIMIZATION-2026-10.md`）
- 渠道侧自研协议栈是最有辨识度的技术资产

但存在**一处硬冲突与多处文档漂移**，共同后果是：**AI 协作与外部评审都会读到失真的项目约束**。

### 1.2 一页纸结论

| 维度 | 评级 | 关键依据 |
|------|------|----------|
| 分层与模块化 | 🟢 优 | `app/{api,services,core,rag,clients,schemas,utils}` + 4 个扩展目录，17 个就近 `backend/app/rag/protocols.py` |
| 接口抽象（§9.3.3） | 🟢 优 | 全仓零业务代码直调 OpenAI/DashScope SDK；`OpenAI(...)` 仅在 `core/qwen.py:98`、`core/embedding.py:109` |
| 配置化（§9.4.2 / §9.6） | 🟢 优 | 12 份业务规则 YAML + 16 份 Prompt YAML；`guard.py:46` 已走 `config_loader` |
| 测试与 CI | 🟢 优 | 1216 passed / 18 skipped；覆盖率 65% ≥ CI 门禁 63%；CI 含 MySQL/Redis/Qdrant service + env diff 检查 |
| 安全与密钥 | 🟢 优 | 无 `.env` 入库、无硬编码 Key、`docs/_private` 已 gitignore |
| 可观测性（§9.5.2） | 🟢 优 | RequestId 中间件、结构化 JSON 日志、`runtime_log`、`/health` 组件级探针 |
| **约束一致性** | 🔴 **差** | AGENTS.md §2 禁止 Milvus，实际默认后端即 Milvus（`deploy/docker-compose.yml:40`） |
| **状态文档时效** | 🔴 **差** | `docs/development/current_status.md` 声明 v10.0/47 commits/392 测试，实际 v18.3+/348 commits/1216 测试 |
| **多租户（§9.4.3）** | 🔴 **缺失** | 19 张表 0 个 `tenant_id` 列；全后端 `tenant_id` 仅 1 处命中 |
| **事件总线（§9.2.4）** | 🟠 缺位 | 全仓无 `EventBus` 符号；仅有闲鱼渠道 Redis `COMMAND_CHANNEL` |
| **单文件体量** | 🟠 超标 | 10 个文件 ≥500 行，最大 1596 行（阈值 200） |
| 文档链接健康度 | 🟠 有瑕疵 | `docs/README.md` 引用 2 个不存在的文件 |

### 1.3 最高优先级的三件事

| # | 事项 | 预估工时 | 收益 |
|---|------|---------|------|
| 1 | **裁决 Milvus 冲突**：改 AGENTS.md §2 + `docs/architecture/system.md` + 补 ADR | 1 h | 消除"规则被自身默认配置违反"的根本矛盾 |
| 2 | **重写 `docs/development/current_status.md` + roadmap 缺口表** | 1.5 h | 修复每次会话首读文件的误导 |
| 3 | **拆分 `backend/app/api/chat.py`**：闲鱼渠道逻辑下沉 `services/xianyu/` | 3 h | 单文件 1109 → 约 700 行，同时消解 §7.2 / §9.2.1 违规 |

---

## 2. 审计方法与可复现命令

### 2.1 方法论

```
静态扫描                      动态验证
├── 目录与体量                ├── pytest（全量 + 覆盖率）
├── 规则命中（§2/§7/§9）     ├── vite build（前端产物）
├── 引用完整性（链接 404）    ├── 密钥与 .env 扫描
└── 接口抽象覆盖度            └── 未提交改动审阅
```

### 2.2 可复现命令

```powershell
# 体量
Get-ChildItem backend/app -Recurse -File -Filter *.py |
  ForEach-Object { [PSCustomObject]@{ L=(Get-Content $_.FullName | Measure-Object -Line).Lines; P=$_.FullName } } |
  Where-Object L -ge 500 | Sort-Object L -Descending

# 测试 + 覆盖率（backend/ 下执行）
python -m pytest tests/ -q --tb=line --cov=app --cov-report=term

# 前端构建（frontend/ 下执行）
node .\node_modules\vite\bin\vite.js build

# 规则命中
rg -n "__tablename__" backend/app/models/          # 租户字段核查
rg -n "import dashscope|OpenAI\(" --type py backend/app/
rg -n "EventBus|event_bus" --type py backend/app/
```

完整命令清单见 [附录 A](#附录-a审计命令清单)。

### 2.3 审计环境

| 项 | 值 |
|----|----|
| 执行日期 | 2026-10-30 |
| Python | 3.11.2544（Windows Store 发行版） |
| Node | v24.19.0（Codex 运行时） |
| Git HEAD | `44ed47d` · docs: 统一对外文档口径 |
| 工作区状态 | 8 文件未提交（+72 / −100） |

---

## 3. 规模实测

### 3.1 代码规模

| 区域 | 文件数 | 行数 | 说明 |
|------|-------:|-----:|------|
| `backend/app` | 237 | 34,519 | 业务主代码 |
| `frontend/src` | 32 | 9,329 | Vue3 + TS（无 Pinia） |
| `backend/tests` | 114 | 20,643 | pytest 套件 |
| `docs/**/*.md` | 65 | 20,772 | 含 learning_log 91 节 / 537 KB |
| **合计（不含测试/文档）** | **269** | **43,848** | |

### 3.2 后端目录分布

```
backend/app/
├── api/           18 文件  ── 路由层（+ xianyu_admin / admin_* 聚合）
├── services/      34 顶层文件 + 19 子目录  ── 业务编排
│   ├── chat/      8 文件（orchestrator 1080 行为主）
│   ├── xianyu/    32 文件（自研渠道栈，最大资产）
│   ├── agents/    5 文件（五专家合成，coordinator + experts + generalist）
│   ├── rag/       4 文件（ingest / pipeline / knowledge）
│   ├── order/ order_modify/ refund/ logistics/ invoice/
│   ├── after_sales/ promotion/ supplement/ dispute/ business_flow/
│   ├── context/ validation/ eval/
│   └── 顶层散装：intent_service / policy_service / guard / metrics ...
├── clients/       mysql / redis / qdrant / milvus / vector_db 门面 / datasource
├── core/          config / logging / security / qwen / embedding / circuit_breaker
│   └── providers/ llm/ embedding/ rerank/ 三个 Protocol 包
├── rag/           protocols / factory / qdrant_impl / milvus_impl
├── schemas/       10 文件 Pydantic 模型
├── models/        19 张 ORM 表
├── tools/         registry（19 个 ToolSpec）+ 6 个 tool 封装
├── channels/      protocols / webhook_impl
└── integrations/  xianyu_config / xianyu_mcp_client
```

### 3.3 数据模型（19 表）

```
用户域    users · user_profiles
会话域    conversations · messages · conversation_contexts · message_ratings
订单域    orders · order_items · refunds
商品域    products · knowledge_documents
运营域    operation_logs · badcase_candidates · confirm_tickets · handoff_tickets
闲鱼域    xianyu_conversations · xianyu_orders · xianyu_items · xianyu_buyer_profiles
```

> ⚠️ 全部 19 表**无 `tenant_id` 列** —— 见 [风险 R2](#r2)

### 3.4 接口抽象覆盖（17 个就近 Protocol）

| 层 | 文件 | 抽象 |
|----|------|------|
| core/providers/llm | `backend/app/rag/protocols.py` | `LLMProvider` |
| core/providers/embedding | `backend/app/rag/protocols.py` | `EmbeddingProvider` |
| core/providers/rerank | `backend/app/rag/protocols.py` | `RerankProvider` |
| rag | `backend/app/rag/protocols.py` | `KnowledgeSource` / `KnowledgeSourceFactory` ⚠️ |
| clients/datasource | `backend/app/rag/protocols.py` | 数据源抽象 |
| channels | `backend/app/rag/protocols.py` | `ChannelAdapter` |
| services/order | `backend/app/rag/protocols.py` | `OrderService` |
| services/order_modify | `backend/app/rag/protocols.py` | `OrderModifyService` |
| services/refund | `backend/app/rag/protocols.py` | `RefundService` |
| services/logistics | `backend/app/rag/protocols.py` | `LogisticsService` |
| services/invoice | `backend/app/rag/protocols.py` | `InvoiceService` |
| services/after_sales | `backend/app/rag/protocols.py` | `AfterSalesService` |
| services/promotion | `backend/app/rag/protocols.py` | `PromotionService` |
| services/supplement | `backend/app/rag/protocols.py` | `SupplementService` |
| services/business_flow | `backend/app/rag/protocols.py` | `BusinessFlow` |
| services/context | `backend/app/rag/protocols.py` | `ContextService` |
| services/dispute | `backend/app/rag/protocols.py` | `DisputeService` |

> ❌ 缺 `VectorStore`（Roadmap G4 推迟至 V3+）、`EventBus`、`EmotionAnalyzer` —— 见 [R3](#r3)

---

## 4. 质量基线实测

### 4.1 测试

```
1216 passed, 18 skipped  in 158.91s
```

| 指标 | 值 | 门禁 | 判定 |
|------|---|------|------|
| 通过率 | 1216 passed | — | 🟢 |
| 覆盖率 | **65%**（14,060 stmt / 4,866 miss） | CI `--cov-fail-under=63` | 🟢 余量 2pp |
| 用例/代码行比 | 1216 / 34,519 ≈ 3.5% | — | 🟢 |

**已知问题**：`backend/tests/test_agent_checkpointer.py` 在**收集期**抛 `ImportError`（`No module named 'langgraph.checkpoint.sqlite'`），导致整个套件收集中断。

- 根因：`langgraph-checkpoint-sqlite>=1.0,<3` 已在 `backend/requirements.txt` 声明，但本机环境未安装
- 影响：**本地**一条命令跑不出结果；**CI** 因 requirements 安装完整不受影响
- 修复：`pip install langgraph-checkpoint-sqlite`（零代码改动）

### 4.2 前端构建

```
✓ built in 10.62s
```

| 产物 | 原始 | gzip | 判定 |
|------|-----:|-----:|------|
| `DataBoard` | **1,135.33 kB** | 381.52 kB | 🔴 超 Vite 500 kB 警告阈值，ECharts 未手动分包 |
| `index` | 120.15 kB | 45.94 kB | 🟢 |
| `ChatPage` | 60.30 kB | 20.75 kB | 🟢 |
| 其余 15 个路由块 | ≤ 10.7 kB | — | 🟢 |

### 4.3 安全扫描

| 检查项 | 结果 |
|--------|------|
| `.env` 类文件入库 | ✅ 无（`git ls-files` 无命中） |
| 硬编码 API Key / Secret | ✅ 无命中 |
| `docs/_private`（简历 / JD / 面试稿） | ✅ 已 `docs/_private/**` gitignore，未入库 |
| `cookie.txt` / `cookies/` | ✅ 已 gitignore，未入库 |
| 根目录临时文件（`findings.md` / `progress.md` / `task_plan.md`） | ✅ 已 gitignore，未入库 |

> 🟢 密钥与隐私卫生无问题。`docs/_private/` 内含 167 KB `docs/_private/jd_brief_for_claude.md` 等个人素材，隔离到位。

### 4.4 CI 门禁

`.github/workflows/ci.yml` 覆盖：

- MySQL 8.0 / Redis 7 / Qdrant 1.10.1 三个 service container
- `pytest --cov=app --cov-fail-under=63`
- **Env diff check**（防止 `backend/app/core/config.py` / `.env.dev` / `deploy/docker-compose.yml` 三方开关漂移）
- 仅 `master` 分支触发，15 分钟超时

---

## 5. 架构现状评估

### 5.1 分层合规（§7.2）

| 层 | 应有职责 | 实测 | 判定 |
|----|---------|------|------|
| `api/` | 路由、参数解析、调 services | 路由为主，但 `backend/app/api/chat.py` 含 Prompt 拼装与 LLM 调用 | 🟠 见 [R4](#r4) |
| `services/` | 业务编排，**禁止直连数据库** | 23 处 `from app.clients.mysql_client/redis_client` | 🟢 见下 |
| `core/` | LLM / Embedding / Rerank | 仅 2 处构造 OpenAI 客户端 | 🟢 |
| `rag/` | 检索 + 生成 pipeline | 顶层 `rag/`（协议层）+ `services/rag/`（实现层）双层 | 🟢 |
| `clients/` | 基础设施客户端 | ✅ 纯客户端 + `vector_db` 门面 | 🟢 |
| `schemas/` | Pydantic 模型 | ✅ 10 文件，无逻辑 | 🟢 |

> **关于 services 直连 clients 的说明**：23 处命中里，绝大多数是**基础设施适配器**（`services/order/mysql_impl.py:16`、`services/refund/mysql_impl.py:16`、`services/rag/ingest.py:22` 等）——这正是 Protocol + 工厂模式的设计意图，**不违规**。真正需要留意的是 `services/context/context_service.py:26` 这类非 impl 模块，属于可接受但需在 Review 时确认的边界。

### 5.2 Provider 抽象（§9.3.3）

```
✅ 全仓零业务代码直接 import dashscope / 调用 Generation.call
✅ OpenAI(...) 构造点仅 2 处，且都在 core/（Provider 实现层）
✅ qwen_provider.py / qwen_provider.py / qwen_provider.py 三件套齐备
✅ 向量库通过 clients/vector_db.py 门面 + importlib 动态转发，切换零代码改动
```

`backend/app/clients/vector_db.py:1` 的门面设计是本仓**最值得保留的架构资产**：

```python
_BACKEND = (settings.VECTOR_DB or "qdrant").strip().lower()
_mod = importlib.import_module(f"app.clients.{_BACKEND}")
```

### 5.3 配置化（§9.4.2 / §9.6）

| 类别 | 数量 | 位置 | 抽样证据 |
|------|-----:|------|---------|
| 业务规则 YAML | 12 | `backend/config/business_rules/` | `backend/config/business_rules/guard.yaml` `backend/config/business_rules/intent.yaml` `backend/config/business_rules/refund.yaml` `backend/config/business_rules/decide.yaml` `backend/config/business_rules/promotion.yaml` … |
| Prompt YAML | 16 | `backend/config/prompts/` | `backend/config/prompts/agent.yaml` `agent_v1/v2` `agent_fc_v1/v2` `query_rewriter/*` `xianyu/*` |
| 配置加载器 | — | `backend/app/services/config_loader.py` | `guard.py:46` → `get_config_loader().load("guard")` |

**残留违规 1 处**：`backend/app/services/rag/pipeline.py:39-45` 仍有内联 `SYSTEM_PROMPT` 字面量。

### 5.4 可观测性（§9.5.2）

| 必记录字段 | 实现 | 判定 |
|-----------|------|------|
| `request_id` | `backend/app/api/middleware.py` RequestIdMiddleware + ResponseHeaderMiddleware | 🟢 |
| `tenant_id` | — | 🔴 见 [R2](#r2) |
| `user_id` / `conversation_id` | 业务层注入 | 🟢 |
| 模型输入输出 / Token 消耗 | `backend/app/services/metrics.py` (461 行) | 🟢 |
| Tool 调用（参数/结果） | `backend/app/tools/registry.py` dispatch | 🟢 |
| 响应时间 | `backend/app/core/runtime_log.py` + 结构化日志 | 🟢 |
| 异常堆栈 | `backend/app/core/logging.py` | 🟢 |

### 5.5 渠道工程（项目最大差异化资产）

`backend/app/services/xianyu/` 32 文件，核心是 `backend/app/services/xianyu/xianyu_ws_client.py`（1596 行）自研协议栈：

| 能力 | 实现位置 | 说明 |
|------|---------|------|
| LWP 帧解码 / MessagePack | `backend/app/services/xianyu/xianyu_ws_client.py` | 零第三方 CLI 依赖 |
| mtop.h5 签名 | `backend/app/services/xianyu/xianyu_token.py` (217 行) | 自研 |
| ackDiff 消息补拉 | `backend/app/services/xianyu/xianyu_ws_client.py` + `backend/app/services/xianyu/xianyu_watcher_poller.py` | 消息不丢 |
| 去重 + 4s 合批 | `backend/app/services/xianyu/debounce_queue.py` | 避免连发打断 |
| Cookie 四层自愈 | `backend/app/services/xianyu/cookie_renew_service.py` (373 行) + `backend/app/services/xianyu/cookie_browser_refresh.py` | mtop 重签 → HTTP 续期 → Playwright 免密 → 人工 |
| 多账号并行 | `backend/app/services/xianyu/account_manager.py` + `backend/app/services/xianyu/account_worker.py` | AccountWorker × N |
| 自家账号防 AI 回环 | `backend/app/services/xianyu/message_guard.py` | — |
| 人工坐席通道 | `backend/app/services/xianyu/admin_bridge.py` + Redis `COMMAND_CHANNEL` | 人工接管 |

---

## 6. 合规对标表

### 6.1 §2 禁止行为（8 条）

| # | 禁止项 | 判定 | 证据 |
|---|--------|------|------|
| 1 | 引入 Kafka / Milvus / Elasticsearch / 新数据库 | 🔴 **违反** | `deploy/docker-compose.yml:40` `VECTOR_DB: ${VECTOR_DB:-milvus}`；`deploy/milvus/{embedEtcd,user}.yaml`；`backend/app/clients/milvus.py`(269行)；`backend/app/rag/milvus_impl.py` |
| 2 | 拆分微服务 | 🟢 合规 | 单一 FastAPI 入口 `backend/app/main.py` |
| 3 | 跨模块写代码 | 🟠 部分 | `api/chat.py:907` `_xianyu_channel_chat` + `:1043` `_xianyu_llm_reply` 落在 API 层 |
| 4 | 一次性设计"未来系统" | 🟠 部分 | `rag/protocols.py:34` `KnowledgeSourceFactory` 全仓**零引用**（占位） |
| 5 | 业务代码/Prompt/密钥硬编码 | 🟠 部分 | `services/rag/pipeline.py:39` 内联 `SYSTEM_PROMPT`；密钥侧 ✅ 无违规 |
| 6 | 修改前未读相关代码 | — | 流程项，代码不可验证 |
| 7 | 无验证提交 | 🟢 合规 | CI 存在且绿；本次实测 1216 passed |
| 8 | 同一报错重试 ≥3 次 | — | 流程项 |

### 6.2 §9 架构设计要求

| 条款 | 判定 | 说明 |
|------|------|------|
| §9.1.1 Interface First | 🟢 | 17 个 Protocol 就近放置 |
| §9.1.2 Module Isolation | 🟠 | `backend/app/api/chat.py` 承载渠道业务 |
| §9.1.3 Dependency Inversion | 🟢 | 依赖方向单向，无循环 |
| §9.2.1 禁止万能模块 | 🟠 | 10 文件 ≥500 行 |
| §9.2.2 禁止跨模块侵入 | 🟢 | 未见 A 直连 B 的内部函数 |
| §9.2.3 依赖单向 | 🟢 | API→Service→Core→Client |
| §9.2.4 事件驱动 | 🔴 | **无 EventBus**；`emotion` 模块不存在 |
| §9.2.5 多轮对话规范 | 🟢 | DebounceQueue + context + resume 齐备 |
| §9.3.1 统一接口 5 件套 | 🟢 | Schema / 异常 / DTO 齐备 |
| §9.3.2 模块可替换 | 🟢 | `vector_db` 门面即范例 |
| §9.3.3 AI 能力抽象 | 🟢 | 零直调 SDK |
| §9.4.1 数据责任隔离 | 🟢 | 19 表按域划分清晰 |
| §9.4.2 配置与逻辑分离 | 🟠 | 1 处内联 Prompt 残留 |
| §9.4.3 多租户 | 🔴 | 0 个 `tenant_id` 列 |
| §9.4.4 数据库分级 | 🟢 | 现有 ALTER 均属 L1 |
| §9.5.1 AI 安全 5 防 | 🟢 | `backend/app/services/guard.py` + `backend/app/services/xianyu/message_guard.py` + `backend/app/services/validation/hallucination_guard.py` |
| §9.5.2 可观测性 | 🟡 | 除 `tenant_id` 外全绿 |
| §9.6 Prompt 独立管理 | 🟠 | 16 YAML + manifest 版本机制 ✅，1 处残留字面量 |
| §9.7 自检 5 问 | 🟡 | 新代码符合度高，历史存量欠账 |
| §9.8 八件套 | 🟢 | `docs/learning_log.md` 91 节 + ADR 8 篇 |

### 6.3 §4.4 Stop-Loss 单文件阈值（200 行）

| 文件 | 行数 | 超出倍数 | 归属 |
|------|-----:|--------:|------|
| `backend/app/services/xianyu/xianyu_ws_client.py` | 1596 | 8.0× | 渠道协议栈 |
| `backend/app/services/xianyu/reply_scheduler.py` | 1162 | 5.8× | 渠道编排 |
| `backend/app/api/chat.py` | 1109 | 5.5× | API + 渠道 + SSE |
| `backend/app/services/chat/orchestrator.py` | 1080 | 5.4× | 应答主流程 |
| `backend/app/services/refund_graph.py` | 743 | 3.7× | LangGraph 退款图 |
| `backend/app/tools/registry.py` | 713 | 3.6× | 工具注册 |
| `backend/app/api/admin_conversations.py` | 583 | 2.9× | 管理聚合 |
| `backend/app/services/business_flow/refund_flow.py` | 568 | 2.8× | 业务流程 |
| `backend/app/services/intent_service.py` | 518 | 2.6× | 意图识别 |
| `backend/app/services/policy_service.py` | 515 | 2.6× | 政策检索 |

### 6.4 文档链接健康度

| 文档 | 引用 | 判定 |
|------|------|------|
| `docs/README.md` | `development/roadmap_v1.md` | 🔴 文件不存在（实际 `docs/development/roadmap.md`） |
| `docs/README.md` | `PROJECT_DESIGN.md` | 🔴 文件不存在 |
| `docs/README.md` | 其余 10 项引用 | 🟢 全部存在 |
| `AGENTS.md` §9.9 路径表 | `app/agents/protocols.py` | 🔴 实际为 `app/services/agents/`（且无 protocols） |
| `AGENTS.md` §9.9 路径表 | `app/clients/vector/protocols.py` | 🔴 实际为 `app/rag/protocols.py` |
| `AGENTS.md` §9.9 路径表 | `app/services/emotion/protocols.py` | 🔴 模块不存在 |
| `AGENTS.md` §7.1 目录表 | 缺 `models/` `tools/` `integrations/` `channels/` | 🔴 实际存在 |
| `docs/development/current_status.md` | 版本/提交数/测试数 | 🔴 全部过期 |
| `docs/architecture/system.md` | 向量库 = Qdrant | 🔴 与实际默认不符 |

### 6.5 双文件镜像同步风险

`AGENTS.md` 与 `CLAUDE.md` 内容 604 行，**仅 8 行差异**（全部是 `AGENTS.md` ⇄ `CLAUDE.md` 名称互换）。二者为手工镜像：

> 🔴 **风险**：修复任一侧的规则必须同步另一侧，否则 Codex 环境与 Claude 环境读到不同约束。建议加一条 CI 检查（diff 归一化后应为空）。

### 6.6 灰度开关三源一致性

抽查 12 个 Feature Flag：

| 开关 | `backend/app/core/config.py` | compose 注入 | `.env.example` | `.env.dev` | 判定 |
|------|:---:|:---:|:---:|:---:|------|
| `ENABLE_AGENT_FC` | ✅ | ✅ | ❌ | ✅ | 🟡 |
| `SSE_CARD_V2` | ✅ | ✅ | ❌ | ❌ | 🟡 |
| `ENABLE_CONTEXT_STORE` | ✅ | ✅ | ❌ | ❌ | 🟡 |
| `ENABLE_BUSINESS_FLOW` | ✅ | ✅ | ❌ | ✅ | 🟡 |
| `ENABLE_ORDER_RESOLVER` | ✅ | ✅ | ❌ | ✅ | 🟡 |
| `ENABLE_ESCALATION_HANDOFF` | ✅ | ✅ | ❌ | ✅ | 🟡 |
| `ENABLE_DUAL_INTENT_DISPATCH` | ✅ | ✅ | ❌ | ✅ | 🟡 |
| `USE_LANGGRAPH_REFUND` | ✅ | ✅ | ❌ | ✅ | 🟡 |
| `ENABLE_PROMPT_VERSIONING` | ✅ | ❌ | ❌ | ❌ | 🔴 compose 未注入 |
| `ENABLE_MULTI_QUERY` | ✅ | ❌ | ❌ | ❌ | 🔴 compose 未注入 |

> 结论：`OPTIMIZATION-2026-10.md` 指出的"开关真值三源漂移"**已部分修复**（compose 注入清单补齐了 8 个），但 `.env.example` 仍缺全部 12 个 Feature Flag 声明，且 2 个开关未进 compose。改这两个开关仍需"重新 build 镜像"，运维陷阱未彻底关闭。

---

## 7. 风险登记册

<a id="r1"></a>

### R1 · Milvus 禁令与实际默认后端冲突

| 项 | 内容 |
|----|------|
| **级别** | 🔴 P0 |
| **违反** | AGENTS.md §2 禁止行为第 1 条；`docs/architecture/system.md` §1.3 + §4 |
| **证据** | `deploy/docker-compose.yml:40` `VECTOR_DB: ${VECTOR_DB:-milvus}`<br>`deploy/docker-compose.yml:41` `MILVUS_URI: http://customer-service-milvus:19530`<br>`deploy/docker-compose.yml:138` `profiles: ["qdrant"]  # 默认停用`<br>`deploy/milvus/embedEtcd.yaml`、`deploy/milvus/user.yaml`<br>`backend/app/clients/milvus.py`（269 行）<br>`backend/app/rag/milvus_impl.py`<br>`backend/app/core/config.py:51` `VECTOR_DB: str = "qdrant"`（默认值与 compose 默认值不一致） |
| **影响** | ① 最高约束文件被自身默认配置违反，规则权威性归零<br>② AI 协作读到失真约束 → 拒绝合理改动 / 做出错误判断<br>③ `backend/app/core/config.py` 默认 `qdrant` 与 compose 默认 `milvus` 不一致，本地直跑与容器行为分叉<br>④ 外部评审对照 README（标 Milvus）与 AGENTS.md（禁 Milvus）直接发现矛盾 |
| **方案 A（推荐）** | 承认迁移为既成事实：改写 AGENTS.md §2 第 1 条为「禁止引入 Kafka / Elasticsearch / 新数据库；向量库限 Qdrant / Milvus 二选一」，`docs/architecture/system.md` 同步补 Milvus 行与双栈迁移说明，补一篇 ADR `docs/decisions/2026-10-30-vector-db-milvus-migration.md` 记录迁移理由与回滚路径 |
| **方案 B** | 回滚默认值为 Qdrant：`deploy/docker-compose.yml` 两处 `VECTOR_DB: ${VECTOR_DB:-qdrant}` + `profiles` 反转，Milvus 保留为可选 profile |
| **工时** | 方案 A：1 h（含 ADR）　方案 B：0.5 h + 重建镜像验证 |
| **验证** | §6 文档类 → 肉眼通读 + `rg -n "Milvus" AGENTS.md docs/architecture/` 核对；方案 B 追加 `docker compose config` + 健康检查 |

<a id="r2"></a>

### R2 · 多租户 `tenant_id` 完全未落地

| 项 | 内容 |
|----|------|
| **级别** | 🔴 P0（面向 SaaS 演进） |
| **违反** | AGENTS.md §9.4.3「新表：默认带 `tenant_id`（默认 `"default"`），查询时不过滤但保留字段」 |
| **证据** | 19 张表 **0** 个 `tenant_id` 列（`rg -n "tenant_id" backend/app/models/` 零命中）<br>全后端 `tenant_id` 仅 1 处命中：`backend/app/tools/registry.py`<br>Roadmap S6（多租户 MVP 预备）从未启动 |
| **影响** | 每新增一张表多欠一分债；未来启用租户过滤需大规模重构（§9.10 明列后果） |
| **方案** | ① 19 表统一加 `tenant_id VARCHAR(32) NOT NULL DEFAULT 'default'`（L1 可空/默认值 ALTER，单 commit）<br>② 查询层**暂不过滤**，仅保留字段（符合 §9.4.3 MVP 策略）<br>③ ORM 模型同步加列 + 默认值<br>④ `.env` 预留 `DEFAULT_TENANT_ID` |
| **工时** | 4–6 h（含迁移脚本 + 测试 + 文档） |
| **验证** | §6 数据库结构 L1 → 迁移脚本 dry-run + `pytest` 回归 + 记录学习日志 |

<a id="r3"></a>

### R3 · 事件总线与 VectorStore 抽象缺位

| 项 | 内容 |
|----|------|
| **级别** | 🟠 P1 |
| **违反** | AGENTS.md §9.2.4（进程内 EventBus 承载跨模块领域事件）、§9.9（`app/core/contracts/event_bus.py`、`app/clients/vector/protocols.py`）、Roadmap G4 |
| **证据** | `rg -n "EventBus" backend/app/` / `rg -n "event_bus" backend/app/` **零命中**<br>仅有闲鱼渠道 Redis `COMMAND_CHANNEL`（`api/xianyu_admin.py:262`）—— 属渠道命令总线，非领域事件总线<br>`services/emotion/` 目录不存在（§9.2.4 事件链首环缺失）<br>`rg -n "class VectorStore"` 零命中；仅有 `rag/protocols.py:9` `KnowledgeSource`（检索语义，非存储语义）<br>`rag/protocols.py:34` `KnowledgeSourceFactory` 全仓零引用 |
| **影响** | ① §9.2.4 的领域事件链（Conversation→Emotion→CustomerService）无法落地<br>② `backend/app/services/policy_service.py` 等仍直连向量库，未来换后端需改 N 处<br>③ `KnowledgeSourceFactory` 是典型 YAGNI 占位，违反 §2 第 4 条 |
| **方案** | ① **删** `KnowledgeSourceFactory` Protocol（零引用，YAGNI 清理，10 分钟）<br>② 抽 `VectorStore` Protocol 到 `app/clients/vector/protocols.py`（当前 2 个实现 qdrant/milvus，**已满足 §3.3「≥2 个实现才抽」门槛**）<br>③ 领域事件按需再做——**当前无第二个消费者，不应现在造 EventBus**（YAGNI）；待 emotion 模块立项时再引入 |
| **工时** | ① 0.2 h　② 3–4 h　③ 不做（记录为 backlog） |
| **验证** | pytest 回归 + Protocol 抽象测试 |

<a id="r4"></a>

### R4 · 单文件体量超标与 API 层业务逻辑溢出

| 项 | 内容 |
|----|------|
| **级别** | 🟠 P1 |
| **违反** | §4.4 Stop-Loss「每个模块改动 < 200 行」；§7.2「api/ 禁止写业务逻辑」；§9.2.1「禁止万能模块」 |
| **证据** | 10 文件 ≥500 行（明细见 §6.3）<br>`backend/app/api/chat.py` 1109 行，含 `<br>　`:907` `async def _xianyu_channel_chat(...)`<br>　`:1043` `async def _xianyu_llm_reply(...)`（直接调 LLM）<br>　`:1063` 手工拼 `system_prompt` 字符串<br>　`:1112` `def _xianyu_inject_context(payload)` |
| **影响** | ① 渠道逻辑与 Web 渠道耦合，闲鱼改动必然触碰 API 层<br>② 单端点 `chat()` 跨约 600 行，改动风险高 |
| **方案（最小改动 · 优先做）** | 把 `chat.py:907-1140` 的闲鱼专属逻辑（`_xianyu_channel_chat` / `_xianyu_llm_reply` / `_xianyu_fallback_text` / `_xianyu_inject_context`）下沉到 `backend/app/services/xianyu/channel_chat.py`，`backend/app/api/chat.py` 仅保留一次调用<br>预期：`backend/app/api/chat.py` 1109 → 约 700 行；**单模块改动，不触碰 services/xianyu 其余 31 文件** |
| **工时** | 3 h（含回归测试） |
| **验证** | §6 业务逻辑 → pytest 单测 + 集成测试全绿；`pytest --cov=app` 无回归 |

> ⚠️ **不建议**在本轮动 `backend/app/services/xianyu/xianyu_ws_client.py`(1596) 与 `backend/app/services/xianyu/reply_scheduler.py`(1162)——协议栈与编排耦合深，重构收益低于风险，应单独立项并配契约测试。

<a id="r5"></a>

### R5 · 状态文档与治理基线严重过期

| 项 | 内容 |
|----|------|
| **级别** | 🔴 P0 |
| **证据** | `docs/development/current_status.md` 头部声明"2026-07-17 · v10.0"、"47 个 commit 已提交"、"pytest 392 passed"<br>实际：348 commits / 1216 passed / README badge `v18.3.0` / git log 含 V21 工作<br>`docs/architecture/system.md` 最后更新 2026-07-11，仍写"Qdrant · 9 表"<br>`docs/development/roadmap.md` §2 缺口清单（G1–G13）未标记闭合，但 G1/G2/G5/G6/G7/G8/G10 在 learning_log 中已完成<br>`docs/README.md` 最后更新 2026-07-11，引用 2 个不存在文件 |
| **影响** | `docs/development/current_status.md` 是**指定的首读文件**（"下次启动开发时先读"）→ 每次会话开场即被误导；外部评审读到 2026-07 的版本与数字 |
| **方案** | ① `docs/development/current_status.md` 重写为 v18.3+/V21 口径，删除"会话级恢复记忆"里的过期 commit 流水（压缩为索引 + 指针）<br>② `docs/architecture/system.md` 补 Milvus 双栈 + 更新表数量/组件清单<br>③ `docs/development/roadmap.md` §2 缺口表逐行标注 ✅ 闭合 + 指向对应 Sprint/learning_log 章节<br>④ `docs/README.md` 修 2 个坏引用 + 更新索引<br>⑤ `docs/README.md` 收录本文档 |
| **工时** | 2.5 h |
| **验证** | §6 文档类 → 肉眼通读 + 链接 404 检查（`rg` 校验文中路径存在） |

> ✅ **落地备注（2026-10-30 · 本报告落地时已同步修复）**
>
> 以下 4 项随本报告一并处理完毕，其余 R5 子项仍待批次一执行：
>
> | 已修复项 | 位置 |
> |----------|------|
> | `roadmap_v1.md` 坏引用 → `roadmap.md` | `docs/README.md` §1.4、§2 目录树 |
> | `PROJECT_DESIGN.md` 坏引用（文件不存在）→ 移除条目 | `docs/README.md` §2 目录树 |
> | 「roadmap V2 待生成」过期标注 → 改为已落地 | `docs/README.md` §0、§5 |
> | 收录本报告到文档索引 | `docs/README.md` §2、§5 |
>
> **仍待处理**：`current_status.md` 重写、`system.md` 组件清单更新、`roadmap.md` §2 缺口表标注闭合状态。

<a id="r6"></a>

### R6 · 灰度开关三源漂移未彻底关闭

| 项 | 内容 |
|----|------|
| **级别** | 🟠 P1 |
| **违反** | `OPTIMIZATION-2026-10.md` §1「开关真值三源漂移」 |
| **证据** | `.env.example` **零** Feature Flag 声明（12 个开关全缺）<br>`ENABLE_PROMPT_VERSIONING`、`ENABLE_MULTI_QUERY` 未进 compose 注入清单<br>`core/config.py:51` `VECTOR_DB` 默认 `"qdrant"` vs compose 默认 `milvus` |
| **影响** | 改开关需重 build 镜像，运维易漏；本地直跑与容器行为分叉 |
| **方案** | ① `.env.example` 补全 12 个 Feature Flag（含注释说明 + 默认值）<br>② compose 补注入 2 个缺失开关<br>③ `backend/app/core/config.py` 的 `VECTOR_DB` 默认值与 compose 对齐（消除分叉）<br>④ CI Env diff check 扩展到 Feature Flag 维度 |
| **工时** | 1.5 h |
| **验证** | §6 配置 → `docker compose config` + grep 引用无遗漏 |

<a id="r7"></a>

### R7 · 前端工程化欠账

| 项 | 内容 |
|----|------|
| **级别** | 🟡 P2 |
| **证据** | 无 `stores/`（AGENTS.md §7.4 列 Pinia），仅 3 个 composable（`useAdminAnalytics` / `useSseContext` / 1）<br>16 个 view 全部内含业务逻辑：`frontend/src/views/DemoLanding.vue` 665、`frontend/src/views/ChatPage.vue` 603、`frontend/src/views/XianyuWorkbench.vue` 534、`frontend/src/views/ProductDetail.vue` 538、`frontend/src/components/OrderCard.vue` 488<br>`DataBoard` chunk 1,135 kB → Vite 大包警告<br>`frontend/src/api.ts` 单文件 700 行 |
| **影响** | 首屏加载与可维护性；view 层难测 |
| **方案** | ① `frontend/vite.config.ts` 加 `manualChunks` 拆 ECharts（10 分钟，直接消警告）<br>② `frontend/src/api.ts` 按域拆为 `api/{chat,shop,admin,xianyu}.ts`<br>③ 状态复杂后再评估 Pinia（**当前不急**——YAGNI，勿提前引入） |
| **工时** | ① 0.2 h　② 1.5 h　③ 不做 |
| **验证** | §6 前端 UI → build + 浏览器肉眼检查 + 无控制台报错 |

<a id="r8"></a>

### R8 · 本地测试环境阻断

| 项 | 内容 |
|----|------|
| **级别** | 🟡 P2 |
| **证据** | `tests/test_agent_checkpointer.py:16` `from langgraph.checkpoint.sqlite import SqliteSaver`<br>本机 `langgraph-checkpoint-sqlite` 未安装 → **收集期** ImportError → 全套件中断<br>`backend/requirements.txt` 已正确声明 `langgraph-checkpoint-sqlite>=1.0,<3` |
| **影响** | 新人 clone 后第一条 `pytest` 命令即失败，误判项目状态 |
| **方案** | ① `pip install -r backend/requirements.txt -r backend/requirements-dev.txt`（零代码改动）<br>② 长期：把该模块的导入改为 `pytest.importorskip` 或加 `collect_ignore`，避免可选依赖硬阻断整套件 |
| **工时** | ① 0　② 0.3 h |
| **验证** | pytest 全量收集通过 |

<a id="r9"></a>

### R9 · `AGENTS.md` ⇄ `CLAUDE.md` 镜像同步风险

| 项 | 内容 |
|----|------|
| **级别** | 🟡 P2 |
| **证据** | 两文件均 604 行，`Compare-Object` 差异 8 行（全部为名称互换） |
| **影响** | 改一侧忘另一侧 → 不同 AI 环境读到不同约束；本次 Milvus 裁决必须同步两处 |
| **方案** | ① 本轮所有规则修改**同时**改两个文件<br>② CI 增加镜像一致性检查（归一化 CLAUDE.md/AGENTS.md 后 diff 应为空） |
| **工时** | ① 随 R1/R5 一并　② 0.5 h |
| **验证** | CI 新增 job |

### 风险矩阵

|  | **影响大** | **影响中** | **影响小** |
|---|---|---|---|
| **紧迫** | 🔴 R1 · R5 | 🟠 R6 | 🟡 R7 · R8 |
| **不紧迫** | 🔴 R2 | 🟠 R3 · R4 | 🟡 R9 |

---

## 8. 修复路线图

### 8.1 批次一 · 治理一致性（建议本周内完成）

> 目标：**让约束文件重新可信**。全部为文档/配置改动，零业务代码变更，风险极低。

| # | 事项 | 关联风险 | 触及文件 | 工时 |
|---|------|---------|---------|-----:|
| 1.1 | **裁决 Milvus** | R1 | `AGENTS.md` §2 + `CLAUDE.md` §2（同步） | 0.5 h |
| 1.2 | 补 ADR：向量库 Milvus 迁移 | R1 | 新建 `docs/decisions/2026-10-30-vector-db-milvus-migration.md` | 0.5 h |
| 1.3 | `docs/architecture/system.md` 更新组件清单与双栈说明 | R1 · R5 | `docs/architecture/system.md` | 0.5 h |
| 1.4 | **重写 `docs/development/current_status.md`** | R5 | `docs/development/current_status.md` | 1 h |
| 1.5 | roadmap 缺口表标注闭合状态 | R5 | `docs/development/roadmap.md` §2 | 0.5 h |
| 1.6 | `docs/README.md` 修坏引用 + 收录本文档 | R5 | `docs/README.md` | 0.3 h |
| 1.7 | `.env.example` 补 12 个 Feature Flag | R6 | `deploy/.env.example` | 0.5 h |
| 1.8 | compose 补 2 个缺失开关注入 | R6 | `deploy/docker-compose.yml` | 0.2 h |
| 1.9 | `backend/app/core/config.py` 的 `VECTOR_DB` 默认与 compose 对齐 | R6 | `backend/app/core/config.py:51` | 0.1 h |
| 1.10 | AGENTS.md §7.1 目录表 + §9.9 路径表对齐真实目录 | R5 | `AGENTS.md` + `CLAUDE.md`（同步） | 0.3 h |
| 1.11 | 加 CI 镜像一致性检查 | R9 | `.github/workflows/ci.yml` | 0.5 h |
| | **小计** | | | **5.0 h** |

> 💡 1.1 + 1.10 涉及 AGENTS.md/CLAUDE.md —— **必须两文件同步修改**，建议同一 commit。

### 8.2 批次二 · 结构收敛（可并行）

| # | 事项 | 关联风险 | 工时 |
|---|------|---------|-----:|
| 2.1 | 删 `KnowledgeSourceFactory` 占位 Protocol | R3 | 0.2 h |
| 2.2 | 抽 `VectorStore` Protocol（`app/clients/vector/protocols.py`） | R3 | 3–4 h |
| 2.3 | `backend/app/api/chat.py` 闲鱼逻辑下沉 `services/xianyu/channel_chat.py` | R4 | 3 h |
| 2.4 | `backend/app/services/rag/pipeline.py` 内联 Prompt 外提到 `config/prompts/` | R5 | 0.5 h |
| 2.5 | 前端 ECharts `manualChunks` | R7 | 0.2 h |
| 2.6 | `frontend/src/api.ts` 按域拆分 | R7 | 1.5 h |
| 2.7 | 可选依赖改为 `importorskip` | R8 | 0.3 h |
| | **小计** | | **8.7–9.7 h** |

### 8.3 批次三 · 能力补齐（需单独立项）

| # | 事项 | 关联风险 | 说明 | 工时 |
|---|------|---------|------|-----:|
| 3.1 | 19 表补 `tenant_id` + 查询层预留 | R2 | 走 §9.4.4 **L1** 流程 | 4–6 h |
| 3.2 | 领域 EventBus + emotion 模块 | R3 | **YAGNI 暂不做**，待 emotion 有真实第二消费者时再启动 | backlog |
| 3.3 | `backend/app/services/xianyu/xianyu_ws_client.py` / `backend/app/services/xianyu/reply_scheduler.py` 拆分 | R4 | 需先补契约测试，不在本轮 | backlog |
| | **小计（不含 backlog）** | | | **4–6 h** |

### 8.4 跨模块改动例外路径（§4.2 四要素）

批次二中的 2.2（VectorStore Protocol）与 2.3（chat.py 拆分）触发跨模块条件，四要素：

| # | 必填项 | 内容 |
|---|--------|------|
| 1 | **业务原因** | ① 向量库已有 Qdrant/Milvus 两个实现，必须有统一契约才能保证业务代码不感知后端；② 闲鱼渠道逻辑与 Web 渠道同处 API 层，任一渠道改动都要触碰另一渠道，违反 §9.2.1 |
| 2 | **接口变化** | ① 新增 `app/clients/vector/protocols.py` 定义 `VectorStore`（ensure_collection / upsert / search / delete / health_check）；② `backend/app/api/chat.py` 删除 4 个 `_xianyu_*` 函数，改为调用 `services/xianyu/channel_chat.py` |
| 3 | **影响范围** | ① `clients/` + `rag/` + `backend/app/services/policy_service.py`（3 个模块）；② `api/` + `services/xianyu/`（2 个模块）——**两项独立实施，互不阻塞** |
| 4 | **隔离策略** | ① 先在 `clients/vector/protocols.py` 定义契约，`backend/app/clients/vector_db.py` 门面实现之，逐个替换调用方（每次一个）；② 先写 `services/xianyu/channel_chat.py` 的单元测试（mock LLM），再迁移调用，最后删除 `backend/app/api/chat.py` 中的旧函数 |

### 8.5 建议排期

```
第 1 周   批次一（5.0 h，全文档/配置）  ──► 恢复约束可信度
第 2 周   批次二（8.7 h）              ──► 收敛结构欠账
第 3-4 周 批次三 3.1（4-6 h）          ──► 打开 SaaS 门
持续      backlog 3.2 / 3.3             ──► 待真实需求触发
```

---

## 9. 回归与验证计划

### 9.1 按变更类型的验证分级（对齐 §6）

| 变更类型 | 本轮对应 | 验证手段 | 通过判据 |
|---------|---------|---------|---------|
| 文档 / 注释 | 批次一 1.1-1.6、1.10 | 肉眼通读 + 链接 404 检查 | 无错字；引用文件均存在 |
| 配置 / `.env.example` | 1.7、1.8、1.9 | `docker compose config` + grep 引用 | 变量名一致；无遗漏 |
| Prompt / 安全规则 | 2.4 | 单测 + 黄金用例 ≥3 条 | 全部命中预期 |
| API 路由 / Schema | 2.3 | curl 命中 + 状态码 + payload | 200/正确字段；异常路径 4xx |
| 业务逻辑 | 2.2、2.3 | pytest 单测 + 集成测试 | 全绿；覆盖正常/异常/边界 |
| 数据库结构 | 3.1 | 迁移脚本 dry-run + 回滚演练 | L1 流程：仅 dev 验证 |
| 部署 / 容器 | 1.7-1.9 | `docker compose config` + 启动 | 健康检查通过 |
| 前端 UI | 2.5、2.6 | build + 浏览器肉眼检查 | 无控制台报错；chunk 警告消失 |

### 9.2 回归基线（执行批次二、三时必须满足）

```powershell
# 后端
cd backend
python -m pytest tests/ -q --cov=app --cov-report=term --cov-fail-under=63
# 基线：1216 passed, 18 skipped, coverage 65%
# 注意：先 pip install langgraph-checkpoint-sqlite（R8）

# 前端
cd frontend
node .\node_modules\vite\bin\vite.js build
# 基线：✓ built in 10.62s，DataBoard chunk ≤ 400 kB（拆包后）
```

### 9.3 每批次收尾检查（§4.5 五项）

| # | 检查项 |
|---|--------|
| 1 | AGENTS.md §2 禁止行为：是否违反任何一条？ |
| 2 | 跨模块耦合：是否有模块 A 直接访问模块 B 的具体实现？ |
| 3 | YAGNI：是否有为"未来"写的空实现 / 占位代码？ |
| 4 | 安全 / 合规 / 密钥：是否有硬编码 / 未脱敏？ |
| 5 | 接口影响：是否破坏其他模块依赖的接口签名？ |

---

## 附录 A：审计命令清单

```powershell
# ---- 规模 ----
Get-ChildItem backend/app -Recurse -File -Filter *.py |
  ForEach-Object { [PSCustomObject]@{ L=(Get-Content $_.FullName | Measure-Object -Line).Lines; P=$_.FullName } } |
  Where-Object L -ge 500 | Sort-Object L -Descending

# ---- 规则命中 ----
rg -n "import dashscope|Generation\.call|OpenAI\(" --type py backend/app/     # §9.3.3
rg -n "__tablename__" backend/app/models/                                       # §9.4.3 租户
rg -n "tenant_id" --type py backend/app/                                        # §9.4.3
rg -n "EventBus|event_bus" --type py backend/app/                               # §9.2.4
rg -n "class VectorStore" backend/app/                                          # §9.3.2
rg -n "SYSTEM_PROMPT\s*=" --type py backend/app/                                # §9.6
rg -n "get_config_loader|config_loader" backend/app/services/guard.py           # §9.4.2

# ---- 结构 ----
Get-ChildItem backend/app -Recurse -Filter protocols.py | ForEach-Object { $_.FullName }
Get-ChildItem backend/app/models -File | Select-Object Name

# ---- 镜像一致性 ----
Compare-Object (Get-Content CLAUDE.md) (Get-Content AGENTS.md)

# ---- 开关三源 ----
rg -n "ENABLE_|USE_LANGGRAPH|SSE_CARD" backend/app/core/config.py
rg -n "ENABLE_|USE_LANGGRAPH|SSE_CARD" deploy/docker-compose.yml
rg -n "ENABLE_|USE_LANGGRAPH|SSE_CARD" deploy/.env.example

# ---- 链接健康 ----
rg -n "\]\(([^)]+\.md)\)" docs/README.md
Test-Path docs/development/roadmap_v1.md ; Test-Path docs/PROJECT_DESIGN.md

# ---- 密钥 ----
git ls-files | rg -i "\.env$|secret|credential|\.pem$"
rg -n "sk-[a-zA-Z0-9]{20,}" --type py backend/app/

# ---- 动态验证 ----
cd backend; python -m pytest tests/ -q --cov=app --cov-report=term
cd frontend; node .\node_modules\vite\bin\vite.js build
```

---

## 附录 B：证据索引

| 主题 | 位置 |
|------|------|
| 向量库默认切换点 | `deploy/docker-compose.yml:40,41,138,285,286` |
| 向量库门面 | `backend/app/clients/vector_db.py:1-33` |
| 向量库配置项 | `backend/app/core/config.py:48-52,258-260` |
| Provider 抽象 | `backend/app/core/providers/{llm,embedding,rerank}/protocols.py` |
| 唯一 OpenAI 构造点 | `backend/app/core/qwen.py:98`、`backend/app/core/embedding.py:109` |
| 业务规则加载 | `backend/app/services/guard.py:35,42,46` |
| 内联 Prompt 残留 | `backend/app/services/rag/pipeline.py:39-45` |
| 占位 Protocol | `backend/app/rag/protocols.py:34` |
| KnowledgeSource 工厂 | `backend/app/rag/factory.py:1-32` |
| 闲鱼逻辑溢出 API 层 | `backend/app/api/chat.py:907,1043,1099,1112` |
| 工具注册（19 ToolSpec） | `backend/app/tools/registry.py` |
| 领域事件缺席 | `backend/app/api/xianyu_admin.py:262`、`backend/app/services/xianyu/admin_bridge.py:121-124` |
| 灰度开关定义 | `backend/app/core/config.py:51,125,164` |
| 文档首读文件（过期） | `docs/development/current_status.md:1-9` |
| 架构文档（过期） | `docs/architecture/system.md:1,30-40` |
| 路线图缺口表 | `docs/development/roadmap.md` §2 |
| 文档索引（坏引用） | `docs/README.md` §2、§5 |
| 自审方案 | `docs/OPTIMIZATION-2026-10.md` §0-§2 |
| 学习日志 | `docs/learning_log.md`（91 节） |
| 镜像约束文件 | `AGENTS.md` / `CLAUDE.md`（均 604 行，8 行差异） |

---

## 附录 C：本次审计未覆盖项

诚实声明本报告的边界，避免被当作全量结论使用：

| 未覆盖 | 原因 |
|--------|------|
| 前端运行时行为（浏览器实测） | 未启动完整环境，仅验证 build 通过 |
| 闲鱼渠道真实连通性 | 需真实账号与 Cookie，未执行 |
| LLM 真实调用质量 | 未消耗 API 额度 |
| Milvus / Qdrant 双栈性能对比 | 未做压测 |
| `scripts/`、`_research/`、`deploy/tests/` 深度审查 | 非生产代码 |
| 前 30 个 commit 的历史合规性 | 仅审计当前状态 |

---

*文档结束 · 2026-10-30 · 审计基线 `44ed47d`*
