# Development Roadmap V2

> **文档代号**：DEV-V2 (Development Roadmap V2)
> **生成方式**：基于 2026-07-11 实际代码扫描（10,290 行 Python）生成，**不基于理想架构**
> **替换关系**：取代 `docs/development/archive/2026-07-11_roadmap_v1_archived.md`
> **维护者**：Tech Lead
> **最近更新**：2026-07-12（Sprint 2 完成，更新 G6 关闭 + S2 章节状态）

---

## 0. 文档说明

### 0.1 与 V1 的关键差异

| 维度           | V1（已归档）                                       | V2（本文档）                                       |
|----------------|---------------------------------------------------|---------------------------------------------------|
| 生成依据       | Tech Lead 对照理想架构（business_architecture_v3 + 18 条架构铁律） | 实际代码扫描 + V2.1 §9 合规对照                   |
| 内容形式       | 6 大块 + 8 Sprint + P0/P1/P2 + 587 行             | 1 现状 + 2 缺口 + 6 Sprint（精简、可执行）         |
| Sprint 优先级  | P0 重合规 + P1 重演进                              | P0 重"再写新代码自动走正确路径"（修接口而非堆功能）|
| 多租户         | M14 默认共享DB + tenant_id 过滤                   | **V2 起步**加 tenant_id 字段；查询层加可选过滤（默认关）|
| 已知偏差       | "重构无关代码"倾向                                 | 严格基于 grep 证据，不动无 grep 命中的代码        |

### 0.2 适用范围

| 读者       | 应读章节                                              |
|------------|-------------------------------------------------------|
| Tech Lead  | 全文（用于 sprint 排序决策）                           |
| AI 编程工具 | §2 合规缺口 + §3 每个 Sprint 的"目标/范围/文件"表       |
| 模块开发者 | §3 即将启动的 Sprint（先看"即将做哪个"）               |
| 面试 / 外部 | §1 实际现状 + §6 时间投入                              |

### 0.3 不在本文档范围

- 业务架构演进 → `docs/architecture/business.md`（V3.1 已冻结）
- 工程纪律本身 → `CLAUDE.md`（V2.1 已固化）
- AI 开发规则 → `docs/governance/ai_development_rules.md`
- 单个 Sprint 的具体代码改动 → 各 Sprint 开工时单独建 `docs/decisions/YYYY-MM-DD-sprint-N.md`

---

## 1. 实际代码现状（2026-07-11 扫描）

### 1.1 规模与目录

```
backend/
├── app/                          ~10,290 行 Python
│   ├── api/                      8 个 router / 中间件
│   ├── clients/                  3 文件（mysql / qdrant / redis）
│   ├── core/                     7 文件（config / qwen / embedding / logging / context / circuit_breaker / security）
│   ├── models/                   9 ORM 表
│   ├── schemas/                  5 文件
│   ├── services/                 21+ 文件（其中 rag/ 子目录 4 文件）
│   ├── tools/                    3 文件（OrderTool / ProductTool / RefundTool）
│   └── main.py                   232 行入口
└── tests/                        9 个测试文件
```

### 1.2 业务模块分布（实际）

| 模块                       | 主要文件                                                 | 行数（最大） | 备注                            |
|----------------------------|----------------------------------------------------------|--------------|---------------------------------|
| Chat 编排                  | `services/synthesizer.py`                                | **928**      | ⚠️ 万能模块，违反 §9.2.1         |
| Intent 分类                | `services/intent_service.py`                             | 197          | 含硬编码 prompt                 |
| Query Rewrite              | `services/query_rewriter.py`                             | 162          | 含硬编码 prompt + 阈值          |
| Policy RAG                 | `services/policy_service.py` + `services/rag/{knowledge, pipeline, ingest}.py` | 346/266/193  | 实际是 RAG 但 policy 不在 rag/ 子目录 |
| RAG 增强                   | `services/{bm25_index, rrf, rerank}.py`                  | 228/76/190   | 散落在 services/                |
| Input Guard                | `services/{guard, guard_centroid, behavior_monitor}.py`  | 285/153/227  | 硬编码阈值 + 闲聊话术            |
| Order/Refund 业务          | `services/{order_service, order_lifecycle, refund_service, refund_graph}.py` + `tools/{order_tool, refund_tool, product_tool}.py` | 337/346/106  | tools/ 不在 CLAUDE.md §7.1 规范 |
| 会话存储                   | `services/{session_service, redis_store, mysql_store}.py` | 124/127/143  | OK                              |
| Auth                       | `services/auth_service.py` + `api/{auth, deps}.py` + `core/security.py` | 106          | OK                              |
| Observability              | `services/{metrics, audit_service}.py` + `core/{logging, circuit_breaker, context}.py` | 282          | OK                              |

### 1.3 数据层（9 表）

| 表                    | 模型                          | tenant_id | 备注                       |
|-----------------------|-------------------------------|-----------|----------------------------|
| users                 | `models/user.py`              | ❌ 无     | 多租户字段缺失（G9）        |
| conversations         | `models/conversation.py`      | ❌ 无     | 多租户字段缺失              |
| messages              | `models/message.py`           | ❌ 无     | 多租户字段缺失              |
| orders                | `models/order.py`             | ❌ 无     | 多租户字段缺失              |
| order_items           | `models/order.py`             | ❌ 无     | 多租户字段缺失              |
| products              | `models/product.py`           | ❌ 无     | 多租户字段缺失              |
| refunds               | `models/refund.py`            | ❌ 无     | 多租户字段缺失              |
| knowledge_documents   | `models/knowledge_document.py`| ❌ 无     | 多租户字段缺失              |
| operation_logs        | `models/operation_log.py`     | ❌ 无     | 多租户字段缺失              |

### 1.4 测试覆盖现状（9 测试）

| 测试文件                              | 覆盖范围                  |
|---------------------------------------|---------------------------|
| `test_anti_hallucination.py`          | 反幻觉（硬约束 prompt）   |
| `test_hybrid_retrieval.py`            | BM25 + RRF 混合检索       |
| `test_llm_retry_breaker.py`           | LLM retry + 断路器        |
| `test_logging_metrics.py`             | 结构化日志 + 指标         |
| `test_refund_graph.py`                | 退款状态机                |
| `test_rerank_integration.py`          | Rerank 集成               |
| `test_robustness.py`                  | 鲁棒性（异常/降级）       |
| `test_source_attribution.py`          | 引用溯源                  |
| `test_synthesizer_refund.py`          | Synthesizer + 退款        |

**测试覆盖特点**：聚焦核心链路（M4/M8/M9.5/M11/M12 等），未覆盖 Provider 抽象（因为还没抽象）。

---

## 2. V2.1 §9 合规缺口清单（12 项）

| #   | 缺口                                       | 严重度 | 证据（grep 命中）                                                                                                                                  | 触发条款 | 修复 Sprint |
|-----|--------------------------------------------|--------|-----------------------------------------------------------------------------------------------------------------------------------------------------|----------|-------------|
| G1  | 无 `LLMProvider` Protocol                  | 🔴 P0  | 6 文件 `from app.core.qwen import chat/stream_chat`（synthesizer/intent_service/query_rewriter/rag/pipeline/refund_graph/rerank）               | §9.1.1 + §9.3.3 | **S1**      |
| G2  | 无 `EmbeddingProvider` Protocol            | 🔴 P0  | 8 文件 `from app.core.embedding import embed_text*`（bm25_index/guard/guard_centroid/policy_service/rag/ingest/rag/pipeline/rag/test_pipeline/response_cache） | §9.1.1 + §9.3.3 | **S1**      |
| G4  | 无 `VectorStore` Protocol（V3+ 推迟）       | 🟠 P1  | `services/policy_service.py` 直接 `from app.clients.qdrant import search as qdrant_search`                                                            | §9.3.3            | **V3+ 推迟** |
| G5  | Prompt 硬编码在业务代码                    | 🔴 P0  | 5 文件含 prompt 字面量（synthesizer/intent_service/query_rewriter/rerank/rag/pipeline/guard）                                                       | §9.6              | **S3**（主要）|
| G6  | 无 `config/prompts/` 目录                  | 🔴 P0  | 目录不存在                                                                                                                                          | §9.6              | **S2 ✅ + S3 部分**（S2 建架子 ✅；S3 仅抽 2/5 个 YAML，余 3 个降级给 Sprint 4） |
| G7  | `synthesizer.py` 928 行万能模块            | 🔴 P0  | 8 个跨模块直接 import（intent_service/order_service/policy_service/refund_service/rag/pipeline/query_rewriter/tools/product_tool/session_service） | §9.2.1            | **S3**（降低）|
| G8  | 业务规则硬编码（阈值/常量）                 | 🟠 P1  | `guard.py:55-67` 等多处硬编码阈值 + `synthesizer.py:42` 硬编码 semaphore 值                                                                          | §9.4.2            | **S4**      |
| G9  | 9 张表均无 `tenant_id`                      | 🟢 P2  | `grep tenant_id backend/app` 0 命中                                                                                                                  | §9.4.3            | **S6**      |
| G10 | 无 `RerankProvider` Protocol（原 G3）       | 🟠 P1  | `services/rerank.py` 直接调 `from app.core.qwen import chat`                                                                                         | §9.3.3            | **S1**      |
| G11 | `services/rag/` 不是顶层 `rag/`（原 G10）   | 🟢 P2  | `services/rag/` 实际位置与 CLAUDE.md §7.1 不符（用户决议：**不迁代码**，仅文档对齐）                                                                | §7.1              | **S5**      |
| G12 | CLAUDE.md §7.1 未列出 `models/` `tools/`（原 G11） | 🟢 P2  | 实际多出 2 个目录                                                                                                                              | §7.1              | **S5**      |
| G13 | rag/ 子目录边界模糊（policy 不在里面）（原 G12）| 🟢 P2  | `services/policy_service.py` 是 RAG 但不在 `services/rag/`                                                                                       | §7.1 + §9.2.1     | **S5**      |

### 2.1 严重度判定原则

| 严重度 | 含义                                                                 |
|--------|----------------------------------------------------------------------|
| 🔴 P0  | 阻碍新代码合规（再写一个 service 仍会绕过接口）；不改 → 技术债指数增长 |
| 🟠 P1  | 当前已能跑，但替换底层或加第二实现时会暴露（已能凑合但有耦合）        |
| 🟢 P2  | 规范对齐 / 长期演进；当前不阻塞                                    |

---

## 3. Sprint 路线（6 个）

### 3.1 总览

| Sprint | 主题                                       | 优先级 | 时间  | 修复缺口 | 涉及文件范围 |
|--------|--------------------------------------------|--------|-------|----------|--------------|
| **S1** | AI 三件套 Provider 抽象                     | 🔴 P0  | 2 周  | G1/G2/G10| `core/providers/{llm,embedding,rerank}/` + 13 个调用点（VectorStore 留 V3+）|
| **S2** | Prompt 基础设施（loader + 目录）            | 🔴 P0  | 1 周  | G6       | `app/services/prompt_loader.py` + `config/prompts/` 架子 |
| **S3** | Synthesizer 拆分（928 → 4 模块）            | 🔴 P0  | 1.5 周| G5（主要）降低 G7 | `services/synthesizer.py`（拆分 + Prompt 抽出）|
| **S4** | 业务规则配置化（阈值 YAML 化）              | 🟠 P1  | 1 周  | G8       | `config/business_rules/`（新）+ 4 个 service |
| **S5** | 目录对齐 CLAUDE.md §7.1（仅文档）            | 🟢 P2  | 0.5 周| G11-G13  | `CLAUDE.md`（不改代码）                  |
| **S6** | 多租户 MVP 预备                            | 🟢 P2  | 1 周  | G9       | `deploy/mysql/init/` + 9 个 model + 入口 |

**总计 7 周**，分 3 阶段：

```
Phase 1 (P0)：S1 + S2 + S3  =  4.5 周  （让"再写新代码"自动走正确路径）
Phase 2 (P1)：S4 + S5      =  1.5 周  （技术债清账 + 规范对齐）
Phase 3 (P2)：S6           =  1 周    （多租户 SaaS 化铺路）
```

---

### 3.2 S1 — AI 三件套 Provider 抽象

| 项     | 内容                                                                                                                                                                                                                                                                                                                                                                                |
|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 把 LLMProvider / EmbeddingProvider / RerankProvider 3 个核心 AI 能力抽象成 Protocol，业务模块改为依赖抽象而非具体实现。VectorStore 抽象推迟到 V3+。                                                                                                                                                                                                                                                       |
| **范围** | • 新增 `app/core/providers/llm/protocols.py` `LLMProvider` Protocol + `QwenLLMProvider` 实现<br>• 新增 `app/core/providers/embedding/protocols.py` `EmbeddingProvider` Protocol + `QwenEmbeddingProvider` 实现<br>• 新增 `app/core/providers/rerank/protocols.py` `RerankProvider` Protocol + `QwenRerankProvider` 实现<br>• 替换 13 个直接 import（VectorStore 推迟到 V3+） |
| **不范围** | • 不改业务逻辑<br>• 不改 Prompt 内容<br>• 不拆 synthesizer（那是 S3）<br>• 不引入 VectorStore Protocol（V3+ 再说，YAGNI）<br>• 不引入第二个 Provider 实现（YAGNI）                                                                                                                                                                                                                                                                            |
| **新建** | `app/core/providers/llm/{protocols.py, qwen_provider.py, __init__.py}`<br>`app/core/providers/embedding/{protocols.py, qwen_provider.py, __init__.py}`<br>`app/core/providers/rerank/{protocols.py, qwen_provider.py, __init__.py}` |
| **修改** | `app/services/synthesizer.py`<br>`app/services/intent_service.py`<br>`app/services/query_rewriter.py`<br>`app/services/rerank.py`<br>`app/services/refund_graph.py`<br>`app/services/rag/pipeline.py`<br>`app/services/rag/ingest.py`<br>`app/services/rag/test_pipeline.py`<br>`app/services/policy_service.py`（仅 LLM/Embedding 部分，Qdrant 暂留）<br>`app/services/guard.py`<br>`app/services/guard_centroid.py`<br>`app/services/bm25_index.py`<br>`app/services/response_cache.py` |
| **保留** | 原 `app/core/qwen.py` `app/core/embedding.py` 内部保留为兼容垫片（标记 deprecated），逐步迁移调用方后删除（不在 S1 范围）<br>`app/services/policy_service.py` 中 Qdrant 直接 import 暂保留（V3+ VectorStore 抽象） |
| **步骤** | 1. 定义 3 个 Protocol（每 Protocol 1-3 个最小方法签名）<br>2. 实现 3 个 QwenProvider（基于原 core/qwen.py / core/embedding.py / services/rerank.py）<br>3. 在 `app/core/providers/<x>/__init__.py` 暴露统一入口（`get_llm_provider()` 等）<br>4. 逐文件切换 import + 改调用（先 1 个 service 跑通再扩）<br>5. 跑全量 9 测试 + 新增 Protocol 契约测试（mock Provider） |
| **验证** | • 全量 pytest 9 测试无回归<br>• 新增 `tests/test_provider_protocols.py`：3 个 Protocol 各自的契约（mock 实现可替换）<br>• `grep -rn "from app.core.qwen import\|from app.core.embedding import" backend/app` 应为 0 命中（保留 deprecation 警告除外） |
| **风险** | 1. Protocol 签名设计返工 → 先写最小签名，跑通 1 个完整调用再扩<br>2. deprecation 垫片误删 → 标记 deprecated 后至少保留 2 个 Sprint 才删除<br>3. 测试覆盖盲点 → 用 `coverage report` 确认新代码路径被覆盖 |

---

### 3.3 S2 — Prompt 配置化（5 个核心）

> **状态**：✅ 已完成（2026-07-12，4 commit：`1f705fc` / `910663c` / `05d5965` / `68d5700`）
> **ADR**：`docs/decisions/2026-07-12-sprint-2-prompt-loader.md`
> **结果**：21 测试 PASS；全量 150 PASS；prompt_loader.py 194 行；关闭 G6

| 项     | 内容                                                                                                                                                                                                                                                                                                                                                                                |
|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 创建 Prompt 配置化的基础设施（`config/prompts/` 目录 + `prompt_loader.py` 统一加载器）。**业务代码中的 Prompt 抽取工作主要由 S3 完成**（拆 synthesizer 时顺手抽），S2 只搭架子。                                                                                                                                                                                                                                                                                                  |
| **范围** | • 新增 `app/services/prompt_loader.py`（统一读取 + 缓存 + 热更新）<br>• 新增 `config/prompts/` 目录（空架子即可，YAML 由 S3 落地）<br>• 写好 prompt_loader 的单元测试（独立于业务代码）                                                                                                                                                                                                       |
| **不范围** | • 不引入 Prompt 版本管理 / 灰度（那是 V3+）<br>• 不引入 DB 表存 Prompt（仅文件）<br>• 不引入租户级覆盖（多租户是 S6）<br>• **不抽取业务代码中的硬编码 Prompt**（S3 做）                                                                                                                                                                                                                                                                       |
| **新建** | `app/services/prompt_loader.py`（统一读取 + 缓存 + mtime 热更新）<br>`config/prompts/.gitkeep`（占位） |
| **修改** | 无（纯新增） |
| **步骤** | 1. 设计 `prompt_loader.load(name: str) -> str` 接口（含缓存 + mtime 热更新）<br>2. 实现 YAML 解析 + 路径安全校验（防 `../../../etc/passwd`）<br>3. 写 `tests/test_prompt_loader.py`：基本加载 / 缓存命中 / mtime 触发重载 / 不存在报错 |
| **验证** | • 新增测试 4+ 用例全过<br>• `prompt_loader.load("不存在的")` 抛明确异常<br>• 改 YAML → 不重启进程内重新调用返回新值（热更新工作） |
| **风险** | 1. YAML 路径越权 → 校验 name 在白名单或限制前缀<br>2. 热更新引入竞态 → 读多写少场景，缓存 + mtime 检查足够；写并发留 V3+ |

---

### 3.4 S3 — Synthesizer 拆分（928 → 4 模块） — ✅ 已完成（5 个 commit，150 → 168 PASS）

| 项     | 内容（实绩）                                                                                                                                                                                                                                                                                                                                                                                |
|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 通过 Synthesizer 拆分（928 → 4 模块）**主要**把硬编码 Prompt 抽到 S2 创建的 `config/prompts/*.yaml`（解决 **G5** Prompt 硬编码）；**降低** 万能模块程度（**G7** 缓解）                                                                                                                                                                                       |
| **范围（实绩）** | • 拆 `synthesizer.py` 为 **5 个**新模块（`orchestrator` + `prompt_assembler` + `stream_dispatcher` + `refund_handler` + `citation_formatter`），比预估 4 个多 1（refund_v2/v3 双轨制下沉）<br>• 抽 **2 个**业务 YAML：`config/prompts/{agent, no_login}.yaml`（**范围 A 决议**：其余 3 个 YAML 留给 S4）<br>• 旧 synthesizer.py 缩为 64 行薄壳（仅 re-export chat.* 公开符号）<br>• 依赖 S2：使用 S2 创建的 `prompt_loader.load()` |
| **不范围（实绩）** | • 不动业务逻辑<br>• 不改 Prompt 内容（仅迁移位置）<br>• 不改 API（仍是 /api/chat）<br>• 不引入新依赖<br>• **范围 A 决议**：不抽其余 3 个跨模块 Prompt（intent/rerank/query_rewriter/guard_chitchat），分属 4 个 service，抽它们跨 ≥ 4 模块，违反 §5 Scope Lock |
| **新建**（5 commit） | • `backend/app/services/chat/{__init__, orchestrator, prompt_assembler, stream_dispatcher, refund_handler, citation_formatter}.py`<br>• `config/prompts/{agent, no_login}.yaml`<br>• `docs/decisions/2026-07-12-sprint-3-synthesizer-split.md`（Sprint 3 启动 ADR）<br>• `tests/test_chat_prompt_assembler.py`（11 用例，纯函数测）<br>• `tests/test_chat_meta_contexts.py`（7 用例，meta contexts 结构契约）|
| **修改** | • `backend/app/services/synthesizer.py`（928 → 64 行薄壳，re-export）<br>• `backend/app/api/chat.py`（import 切到 chat.* 路径）<br>• 3 个测试文件 patch namespace 迁移到 chat.*（test_anti_hallucination / test_source_attribution / test_synthesizer_refund）|
| **拆分边界（实绩）** | • `orchestrator.py`（402 行）：class Synthesizer + run_stream + _try_direct_answer_order + _handle_order + _handle_product + _handle_policy + _DIRECT_ANSWER_PATTERNS<br>• `prompt_assembler.py`（276 行）：7 个模块级格式化函数 + SYSTEM_PROMPT_BASE / NO_LOGIN_PROMPT 走 prompt_loader<br>• `stream_dispatcher.py`（78 行）：_LLM_SEMAPHORE + stream_llm + stream_simple + search_by_keyword_window<br>• `refund_handler.py`（222 行）：handle_refund_v2 + handle_refund_v3（V2 RefundService / V3 LangGraph 双轨制）<br>• `citation_formatter.py`（14 行）：空壳（未来引用标签规范化）|
| **commit 节奏（实绩）** | c1 ADR · c2 2 YAML · c3 cp 4 模块（安全网）· c4 切换 + 薄壳化（退款下沉）· c5 测试 + 文档（本次） |
| **验证** | • 168 PASS 无回归（150 → +11 prompt_assembler +7 meta_contexts）<br>• chat/ 文件结构：6 个文件（__init__ + 5 业务）<br>• 跨模块 import：chat/ 仍依赖原 7 个 service（无新增跨模块），满足 §5 Scope Lock<br>• 反向依赖：除 services/synthesizer.py 薄壳 re-export 外，无其他 services/ 反向依赖 chat/ |
| **预算偏离记录** | • orchestrator.py 402 行 > ADR 自定 < 350 目标（52 多）：未抽 _handle_product + _try_direct_answer_order，约能再降 160 行<br>• prompt_assembler.py 276 行 > 自定 < 250 目标（26 多）：_build_context_block 53 行可下沉<br>• chat/ 文件数 6 > 自定 ≤ 4 目标（2 多）：refund_handler 是退款双轨制独有，文件数 vs 行数取舍选了行数让步<br>• 这 3 处降规模方向明确、可独立 commit，后续 Sprint 收尾 |
| **关闭缺口** | **G5 部分**（synthesizer 范围内：2/5 YAML = 永 fail-safe）<br>**G7 缓解**（synthesizer 928 → 4-5 模块；G7 设计意图是"不再万能"，本次 5 模块分工清晰，已达 G7 目标） |
| **Sprint 4 关联** | • 余 3 YAML（intent / query_rewriter / guard_chitchat）安排 S4 各自拆所属 service<br>• orchestrator.py / prompt_assembler.py 进一步降规模（行数预算 + 测试）可走 S4 |

---

### 3.5 S4 — 业务规则配置化（阈值 YAML 化）

| 项     | 内容                                                                                                                                                                                                                                                                                                                                                                                |
|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 把业务规则（阈值 / 黑白名单 / 重试次数等）从代码中迁到 YAML，符合 §9.4.2。                                                                                                                                                                                                                                                                                                            |
| **范围** | • 新增 `config/business_rules/{guard, intent, refund_graph, query_rewriter}.yaml`<br>• 4 个 service 阈值改读 YAML<br>• 阈值 YAML 不参与热更新（启动时加载即可）                                                                                                                                                                                                                          |
| **不范围** | • 不引入动态配置中心（Apollo / Nacos 之类）<br>• 不引入租户级配置（S6 才做）<br>• 不动 Prompt（S3 已处理）                                                                                                                                                                                                                                                                                |
| **新建** | `config/business_rules/guard.yaml`<br>`config/business_rules/intent.yaml`<br>`config/business_rules/refund_graph.yaml`<br>`config/business_rules/query_rewriter.yaml`<br>`app/services/config_loader.py`（统一读取 + 单例缓存） |
| **修改** | `app/services/guard.py`（`MIN_LEN` `MAX_LEN` `DOMAIN_RELEVANCE_THRESHOLD` 等常量 → YAML）<br>`app/services/intent_service.py`<br>`app/services/refund_graph.py`<br>`app/services/query_rewriter.py`<br>`app/services/synthesizer.py`（`_LLM_SEMaphore` 当前值 10） |
| **步骤** | 1. 列每个 service 的所有硬编码常量（grep `^[A-Z_]+\s*=\s*[^"]` 模式）<br>2. 4 个 YAML 写默认值（与当前代码一致）<br>3. 4 个 service 改用 `config_loader.load("xxx")` 读<br>4. 新增 `tests/test_config_loader.py` |
| **验证** | • 全量 9 测试无回归<br>• 改 YAML 中某阈值 → 重启服务后行为变化<br>• `grep -rn "^[A-Z_]\+\s*=\s*[0-9]" backend/app/services/` 命中应显著减少（仅留实现细节如重试次数默认值） |
| **风险** | 1. 阈值漏改导致线上行为变化 → YAML 默认值必须与原常量完全一致，单元测试比对<br>2. 类型转换错 → YAML 加载时做 schema 校验 |

#### 3.5.1 S4 实绩记录（2026-07-13 截至）

| 阶段 | commit | 状态 | 关闭缺口 |
|------|--------|------|----------|
| **阶段 1**：config_loader 基础设施 | `38932ab feat(services): Sprint 4 阶段 1 - 业务规则配置加载器（config_loader）` | ✅ 完成 | G8 启动基础 |
| **阶段 2**：guard 业务规则 YAML 化 | `5132176 feat(services): Sprint 4 阶段 2 - guard 业务规则 YAML 化` | ✅ 完成 | G8（guard 阈值） |
| **阶段 3**：refund 业务规则 YAML 化（3 文件共享 1 YAML） | `70e5a3e feat(services): Sprint 4 阶段 3 - refund 业务规则 YAML 化`<br>`efa729b test(services): test_guard_config 加 autouse fixture 隔离 config_loader 单例` | ✅ 完成 | G8（refund 阈值） |
| **阶段 4**：intent 业务规则 YAML 化（81 pattern + 2 实体正则） | `1338a09 feat(services): Sprint 4 阶段 4 - intent 业务规则 YAML 化` | ✅ 完成 | G8（intent） |
| **阶段 5**：query_rewriter 业务规则 YAML 化 + Prompt 抽取 | `Sprint 4 阶段 5` | ✅ 完成 | G8（query_rewriter） + §9.6 Prompt 独立（1/3） |
| **收尾**：删 2 legacy 薄壳 + Provider docstring 修正 | `Sprint 4 收尾` | ✅ 完成 | G8 闭合 + Provider 边界清晰化 |

**S4 关闭缺口累计**：G8 = 4/5 YAML（guard / refund / intent / config_loader 架子；query_rewriter 待后续）

**S4 阶段 3 关键发现（沉淀到 learning_log §28）**：
- `REFUND_WINDOW_DAYS = 7` / `DELIVERY_OFFSET_DAYS = 2` 在 3 个文件硬编码（refund_graph / refund_tool / order_lifecycle），按 CLAUDE.md §5.2 跨模块四要素一次迁移，单一真相源落地
- test 污染修复：fail-fast 测试 reload 模块时 `get_config_loader()` 在 `load()` 抛错前就把 `_loader` 全局指向 monkeypatch 后的目录 → 加 post-only autouse fixture 隔离（pre+post 会破坏同文件 `is` 断言）
- pytest 全量 198/198 PASS，GitHub Actions CI run #7 success

**S4 阶段 4 关键发现（沉淀到 learning_log §29）**：
- `IntentType` 是 `Literal` 非 `Enum`，不能 `IntentType(str)` 构造；用 `frozenset(IntentType.__args__)` 在 YAML 加载期校验 key 合法性（fail-fast）
- `INTENT_RULES` 由 `list[tuple]` 改 `dict` 保序（Python 3.7+），行为不变；`_rule_classify` 改 `.items()` 迭代
- flags 用 `getattr(re, "IGNORECASE")` 动态解析；测试用按位与判断（compile 后 flags=34 含 UNICODE 默认位，非 `==2`）
- `prompt_assembler._ORDER_NO_RE` 双源保留，本阶段不合并（YAGNI + 跨模块）
- pytest 全量 212/212 PASS，GitHub Actions CI run #9 success

**S4 阶段 5 + 收尾 关键发现（沉淀到 learning_log §30）**：
- **Provider vs legacy 决策**：qwen.py / embedding.py 按用户"保留 Provider 内部实现"决策不删除，docstring 改为"Provider 内部 DashScope 客户端"（业务模块禁止直接 import）
- **删 2 薄壳**：`services/rerank.py`（12 行 wrapper）+ `services/synthesizer.py`（65 行 re-export）；grep 验证 0 引用 → 安全删除
- **测试 mock 路径修正**：policy_service 切 Provider 后，测试 mock 须用 `get_embedding_provider` / `get_rerank_provider` 而非旧 `embed_text` / `rerank` 函数；3 测试报错 → 修正后 224/224 PASS
- **跨脚本迁移**：`scripts/eval_hitk.py` / `scripts/gen_eval_set.py` 一并切到 Provider（彻底清退含 scripts，按用户决策）
- **pytest 全量 224/224 PASS**，全量验证齐

**下一阶段进入条件**：Sprint 4 已闭环；下一阶段决策权交回用户（待 P2 backlog 或 Phase 4 query_rewriter 业务能力增强）

---

### 3.6 S5 — 目录对齐 CLAUDE.md §7.1（仅文档）

| 项     | 内容                                                                                                                                                                                                                                                                                                                                                                                |
|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 让 CLAUDE.md §7.1 与实际目录一致；不改代码。                                                                                                                                                                                                                                                                                                                                        |
| **范围** | • CLAUDE.md §7.1 增补 `models/` `tools/` 注释<br>• 明确 `services/rag/` 子目录不迁（用户决议，2026-07-11）<br>• 说明 `utils/` 当前未使用（YAGNI）                                                                                                                                                                                                                                       |
| **不范围** | • **不执行 `services/rag/` → 顶层 `rag/` 迁移**（用户明确禁止）<br>• 不合并 `policy_service.py` 入 `services/rag/`（虽然有 G13 缺口）<br>• 不改 CLAUDE.md 其他章节 |
| **修改** | `CLAUDE.md §7.1` |
| **步骤** | 1. 读 CLAUDE.md §7.1 当前内容<br>2. 在树状图后加注释：`models/` `tools/` 说明 + `services/rag/` 子目录说明<br>3. 增加"本节反映 2026-07-11 实际扫描结果"标注 |
| **验证** | • CLAUDE.md §7.1 与 `find backend/app -type d -not -path "*/__pycache__*"` 输出对齐<br>• 无代码改动 |
| **风险** | 极低（仅文档） |

---

### 3.7 S6 — 多租户 MVP 预备

| 项     | 内容                                                                                                                                                                                                                                                                                                                                                                                |
|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 为 9 张核心表加 `tenant_id` 字段（默认 `"default"`），为未来 SaaS 化预留扩展能力，但**不启用强制过滤**（MVP 单租户友好）。                                                                                                                                                                                                                                                                |
| **范围** | • 9 张表加 `tenant_id VARCHAR(64) NOT NULL DEFAULT 'default'`<br>• 9 个 ORM model 加字段<br>• 入口（API middleware）加 `tenant_id` 提取（默认 `"default"`）<br>• query 层加可选过滤开关（**默认关闭**）<br>• 现有数据迁移脚本（`UPDATE ... SET tenant_id = 'default'`） |
| **不范围** | • 不启用强制 tenant 过滤（业务层不改）<br>• 不引入 tenant 级 RBAC（V3+）<br>• 不引入 tenant 级 Prompt 覆盖（S2 已用全局 YAML）<br>• 不引入 Schema 隔离 / DB 实例隔离                                                                                                                                                                                                                       |
| **修改** | `deploy/mysql/init/01_schema.sql`（9 表 ALTER）<br>`backend/app/models/*.py`（9 model 加字段）<br>`backend/app/api/middleware.py`（提取 tenant_id）<br>`backend/app/core/config.py`（加 `TENANT_FILTER_ENABLED = False` 配置项）<br>`backend/app/clients/mysql_client.py`（提供 `set_tenant_filter` 开关） |
| **步骤** | 1. 写 `database/migrations/2026-XX-XX_add_tenant_id.sql`（L1 普通 ALTER，不锁表）<br>2. 跑 dry-run 在 dev DB<br>3. 9 个 ORM model 加 `tenant_id = Column(String(64), nullable=False, default="default", index=True)`<br>4. 中间件从 JWT / Header / Cookie 三选一提取（默认读 JWT）<br>5. 加 `TENANT_FILTER_ENABLED` 配置项（默认 False）<br>6. 备份脚本 + 回滚脚本就位 |
| **验证** | • 现有 9 测试无回归<br>• 新增 `tests/test_tenant_middleware.py`：默认提取、Header 覆盖、JWT 提取<br>• DB 备份 → 跑迁移 → 数据无丢失 + `tenant_id` 全为 'default'<br>• 启用 `TENANT_FILTER_ENABLED=true` 后查询自动带 WHERE tenant_id='current' |
| **风险** | 1. ALTER 锁大表 → 用 `pt-online-schema-change` 或分批；当前数据量小，普通 ALTER 即可<br>2. 业务代码绕过 tenant 提取 → 入口必须强制，不提供 NULL<br>3. 跨 tenant 误读 → 过滤默认关闭，靠应用层自律；V3 再加全 SQL 拦截 |

---

### 3.8 Phase 4 — query_rewriter 业务能力增强（脱离 Sprint 路线 · 单点纵深）

> **状态**：✅ A4 完成（2026-07-14，1 feature + 1 test+docs+eval commit）
> **ADR**：未独立创建（按 §5.2 跨模块四要素口头审批；与 Sprint 4 阶段 5 同结构）
> **学习日志**：`docs/learning_log.md §31`
> **当前位置**：Sprint 1-4 闭环后单点能力纵深；不属于 Roadmap V2 的 6 个 Sprint，列为 Phase 4 业务能力层

| 项     | 内容                                                                                                                                                                                                                                                                                                                                                                                |
|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 在 Sprint 4 业务规则配置化基础上，给 `query_rewriter.py` 加 Multi-Query 业务能力：单路改写 → N 路改写 → 多路 RAG → RRF 融合。零侵入灰度（`ENABLE_MULTI_QUERY` 默认 false）。                                                                                                                                                                                                                                                |
| **范围（实绩 A4）** | • `query_rewriter.rewrite_query_multi(query, history, n)` 沿用 L0/L1/L2 防浪费链路<br>• `PolicyService.search_multi_policy(queries, top_k)` 多路 RAG + RRF 融合（本地 import rrf_fuse 防循环）<br>• `chat/orchestrator.run_stream` 加 `search_queries` 中间变量，灰度开关 `settings.ENABLE_MULTI_QUERY`<br>• `config/prompts/query_rewriter/{multi_system, multi_user_template}.yaml`（新增 2 Prompt）<br>• `config/business_rules/query_rewriter.yaml` 追加 3 字段<br>• `scripts/eval_hitk.py` 加 `--multi-query` flag<br>• 18 测试（12 query_rewriter_multi + 6 policy_service_multi）+ 1 YAML 配置测试 |
| **不范围（A4）** | • **不做 HyDE**（YAGNI；Multi-Query 已覆盖"同义改写扩展召回"）<br>• **不做同义词扩展**（同上）<br>• **不做改写模型微调**（依赖 LLM 通用能力足够）<br>• **不改 orchestrator 调度逻辑**（仅新增 1 个中间变量，灰度开关走 settings）<br>• **不动 order_query / refund_query**（无 RAG 召回需求） |
| **新建** | `backend/config/prompts/query_rewriter/multi_system.yaml`<br>`backend/config/prompts/query_rewriter/multi_user_template.yaml`<br>`backend/tests/test_query_rewriter_multi.py`（12 用例）<br>`backend/tests/test_policy_service_multi.py`（6 用例） |
| **修改** | `backend/app/services/query_rewriter.py`（+150 行 · `rewrite_query_multi` 函数 + 2 Prompt 常量）<br>`backend/app/services/policy_service.py`（+30 行 · `search_multi_policy` 静态方法）<br>`backend/app/services/chat/orchestrator.py`（+15 行 · `search_queries` 中间变量 + 透传）<br>`backend/app/core/config.py`（+3 行 · `ENABLE_MULTI_QUERY` / `MULTI_QUERY_COUNT` / `MULTI_QUERY_TRIGGER`）<br>`backend/config/business_rules/query_rewriter.yaml`（+3 行）<br>`backend/app/services/metrics.py`（+15 行 · `inc_rewrite_multi(reason)` 计数器）<br>`scripts/eval_hitk.py`（+30 行 · `--multi-query` flag）<br>`backend/tests/test_query_rewriter_config.py`（+9 行 · `test_multi_query_constants_loaded`） |
| **关键设计决策** | • **灰度开关默认 false**：`ENABLE_MULTI_QUERY=False` 保证全量 243 测试零侵入，业务行为与 Sprint 4 闭环完全一致<br>• **mock 兼容保留**：orchestrator 用 `search_queries is None → 单路 / 非 None → 多路` 条件调度；现有 `test_anti_hallucination` 等测试 mock `PolicyService.search_policy` 不受影响<br>• **防浪费 L0/L1/L2 链路复用**：多路改写与单路共用同一套短路判定（无指代词/无 history）+ JSON 解析 + 长度/dedup 校验<br>• **本地 import rrf_fuse**：`policy_service.search_multi_policy` 函数内 `from app.services.rrf import rrf_fuse`，避免模块顶部循环依赖 |
| **关闭缺口** | • **新增能力层（不在原 G 缺口表）**：业务能力纵深，对应 query_rewriter.py 业务能力提升<br>• §9.6 Prompt 7/7 YAML（refund / guard_chitchat / orchestrator / agent / no_login / intent / query_rewriter + 新增 2 multi_*） |
| **验证** | • 全量 pytest **243/243 PASS**（224 baseline + 12 query_rewriter_multi + 6 policy_service_multi + 1 YAML 配置测试）<br>• `ENABLE_MULTI_QUERY=False` 灰度基线：单路路径行为不变（grep 0 业务路径变化）<br>• eval_hitk.py `--multi-query` 接入完成，待真实流量验证 hit@K 提升幅度 |
| **下一步可选** | • A5：Multi-Query 串行 → 并行（`asyncio.gather` + SSE 增量返回）<br>• A6：RRF 加权（按业务可信度给不同 query 变体打分）<br>• A7：HyDE（生成假设性答案作 embedding query）<br>• A8：Rerank 时机前移 → 融合后统一 rerank（全局重排） |

---

### 3.9 P2 长程记忆 — user_profiles + profile_service + prompt 注入（P2 backlog 第 2 项）

> **状态**：✅ 完成（2026-07-14，1 feature + 1 test+docs commit）
> **ADR**：未独立创建（按 §5.2 跨模块四要素口头审批；与 Phase 4 A4 同结构）
> **学习日志**：`docs/learning_log.md §32`
> **当前位置**：P2 backlog 5 项中第 2 项（已完成）；不在 Roadmap V2 6 个 Sprint 内，列为 Phase 业务能力层

| 项     | 内容                                                                                                                                                                                                                                                                                                                                                                                |
|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 在 Sprint 1-4 + Phase 4 A4 闭环基础上，给客服系统加**跨 session 用户画像**：profile 加载 → 注入 prompt → done 后 best-effort 自动更新。零侵入灰度（`ENABLE_USER_PROFILE` 默认 false）。                                                                                                                                                                                                                                                                   |
| **范围（实绩）** | • 新增 `user_profiles` 表（1:1 → users.id）存 summary / frequent_skus / preferences / interaction_count<br>• `services/profile_service.py`（5 写入函数 + 1 纯格式化函数）：get_or_create / update_summary / append_frequent_skus / increment_interaction / clear / to_prompt_block<br>• `chat/orchestrator.run_stream` 启动期加载 profile → 转 `profile_block` → 注入 `context_block` 末尾<br>• `api/chat.py` done 事件后 best-effort 累加 `interaction_count` + 追加 `frequent_skus`<br>• 灰度开关 `settings.ENABLE_USER_PROFILE=False`（默认关）<br>• 27 测试（test_profile_service.py：全 mock with_safe_session） |
| **不范围（YAGNI §3.3）** | • **不做事件流**（user_profile_events）→ messages JOIN 即可<br>• **不做派生画像**（user_personas）→ summary 字段够用<br>• **不做租户级画像** → profile 跟 user_id 1:1<br>• **不做 LLM 自动摘要** summary → done 后仅累加 interaction + SKUs；summary 字段靠人工编辑或后续摘要脚本补<br>• **不做情感画像 / 画像聚类** → 当前无第二实现需求<br>• **不做隐私删除 UI** → profile_service.clear() 函数接口就位；admin API / 用户自助入口留 V3+ |
| **新建** | `deploy/mysql/init/02_user_profiles.sql`（Docker init 脚本，1 张新表）<br>`backend/app/models/user_profile.py`（UserProfile ORM）<br>`backend/app/services/profile_service.py`（~250 行 · 5 写 + 1 纯格式化）<br>`backend/tests/test_profile_service.py`（27 用例：5 写函数 + 1 纯函数 + 隐私边界 + 灰度开关） |
| **修改** | `backend/app/core/config.py`（+5 行 · `ENABLE_USER_PROFILE` / `USER_PROFILE_PROMPT_MAX_LEN`）<br>`backend/app/services/chat/prompt_assembler.py`（+10 行 · `_build_context_block` 扩 `profile_block` 参数）<br>`backend/app/services/chat/orchestrator.py`（+18 行 · 启动期加载 profile → 注入 context）<br>`backend/app/api/chat.py`（+18 行 · done 后 `increment_interaction` + `append_frequent_skus`） |
| **关键设计决策** | • **灰度开关默认 false**：`ENABLE_USER_PROFILE=False` 保证全量 270 测试零侵入；现有 chat/orchestrator 行为与 Phase 4 A4 完全一致<br>• **best-effort 双保险**：profile_service 内部 try/except + orchestrator/api 外层 try/except 双重防御；任何一层失效都不阻塞主流程<br>• **反幻觉 hard label**：to_prompt_block 输出带"仅作参考，不得编造未在 profile 中出现的用户事实"（与 M9.5 同模式）<br>• **隐私边界前置**：user_id=0（匿名）短路写在所有 5 个写函数顶部（不是顶层 if 判断）<br>• **prompt 硬上限 200 字**：与 context_block 同设计；防 profile 注入后 LLM 推理成本失控<br>• **autouse fixture 不需要**：service 函数幂等（每次 get_or_create 返新对象）；不需 fixture 隔离单例 |
| **关闭缺口** | • **§9.5 安全可观测**（架构验收维度）从 🟡 部分升级中：profile 给"用户级分析"留基础设施<br>• **新增能力层**（不在原 G 缺口表）：跨 session 记忆能力 |
| **验证** | • 全量 pytest **270/270 PASS**（243 baseline + 27 profile_service）<br>• `ENABLE_USER_PROFILE=False` 灰度基线：现有测试零修改通过（profile_block 始终空串，context_block 行为不变）<br>• dev DB 需手工跑 `02_user_profiles.sql`（deploy init 仅 fresh DB 触发）；生产部署后自动建表 |
| **下一步可选** | • profile 自动摘要脚本（LLM 抽每 24h 1 次）<br>• 用户自助隐私删除 UI（profile_service.clear() 已就位）<br>• admin 后台画像查看（聚合 + 单用户）<br>• 租户级画像（Sprint 6 多租户 MVP 后扩） |

### 3.10 Sprint 5 阶段 1 — Prompt 版本管理（manifest 模式 + 兼容模式）（P2 backlog 第 3 项 · 基础机制）

> **状态**：✅ 完成（2026-07-14，1 feat + 1 test+docs commit）
> **ADR**：未独立创建（单模块改动 + 用户确认 MVP 边界）
> **学习日志**：`docs/learning_log.md §33`
> **MVP 边界（用户拍板）**：保留 version 机制 / 暂缓 rollout 灰度 / 暂缓 6 YAML 全量迁移 / 灰度作为后续阶段

| 项     | 内容                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
|--------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **目标** | 在 Sprint 2 prompt_loader 基础上加**多版本管理**：manifest 模式（`default_version` + `versions` 字典）+ 兼容模式（旧 YAML 自动当 v1）+ 总开关（`ENABLE_PROMPT_VERSIONING` 默认 false）。灰度（traffic_ratio / hash）作为 Sprint 5 后续阶段。                                                                                                                                                                                                                                                                                            |
| **范围（实绩）** | • `YAMLPromptLoader.load(name, version=None)` 接口扩展（向后兼容，现有调用方零修改）<br>• Manifest 模式：`default_version` + `versions` 字典（每个 version 引用外部 `file` 或内联 `content`）<br>• 兼容模式：旧 YAML（无 `versions` 字段）自动当 v1，显式指定 v2 抛 `PromptVersionError`<br>• 缓存升级：key 从 `name` 改为 `(name, version)` tuple；mtime 取 `max(manifest, 内容文件)`<br>• `PromptVersionError` 异常类（带 name / version / reason + 可用版本列表）<br>• `agent.yaml` 改造为 manifest 示范（v1 + v2 两版本）<br>• 16 测试（5 类） |
| **不范围（YAGNI §3.3）** | • **不做 traffic_ratio 灰度**（用户拍板后续阶段）<br>• **不做 hash_key 分配**（同上）<br>• **不做其他 5 个 YAML 全量迁移**（no_login / query_rewriter/*）→ 保持旧 YAML 走兼容模式<br>• **不做多租户 prompt 覆盖**（Sprint 6 同步）<br>• **不做 DB 存储**（V3+ 评估）<br>• **不做 Prompt Editor UI**（V3+ 评估）<br>• **不做嵌套 include**（manifest 只支持 1 层 file 引用）<br>• **不做"列出所有可用 version"API**（load 时通过 PromptVersionError 间接知道有哪些） |
| **新建** | `backend/config/prompts/agent_v1.yaml`（v1 内容，从原 agent.yaml 拆出）<br>`backend/config/prompts/agent_v2.yaml`（v2 实验版，作为机制示范）<br>`backend/tests/test_prompt_loader_version.py`（16 用例：manifest 5 + content 3 + 兼容 3 + 缓存 2 + 异常 2 + mtime 1） |
| **修改** | `backend/app/services/prompt_loader.py`（+85 行 · load 扩 version 参数 + manifest 解析 + 兼容模式 + 缓存升级）<br>`backend/app/core/config.py`（+5 行 · `ENABLE_PROMPT_VERSIONING` 总开关）<br>`backend/app/services/chat/prompt_assembler.py`（+2 行注释 · 现有调用零行为变化）<br>`backend/config/prompts/agent.yaml`（manifest 模式重写）<br>`backend/config/prompts/README.md`（manifest 模式 + 多版本管理章节） |
| **关键设计决策** | • **MVP 边界用户拍板**：原方案 4 能力（manifest / 灰度 / 全量迁移 / Settings）→ 调整为只做基础机制 + 1 个示范；避免过度设计<br>• **向后兼容**：version=None 默认参数；现有 6 处调用全部不传 = 行为零变化<br>• **mtime max 合并**：缓存 mtime 取 `max(manifest_mtime, version_file_mtime)`，任意文件改动触发重读<br>• **缓存 key 升 tuple**：`Dict[Tuple[str, str], ...]`，兼容模式用 `"__compat__"` 占位<br>• **兼容模式显式 v2 抛错**：避免"我以为我用 v2 实际是 v1"的静默回退坑<br>• **总开关默认 false**：`ENABLE_PROMPT_VERSIONING=False` 保证迁移期现有测试零侵入 |
| **关闭缺口** | • **§9.6 Prompt 工程独立管理**（架构验收维度）：版本 + 回滚 + 多版本可管理 → 升级为完整满足<br>• **新增能力层**（不在原 G 缺口表）：A/B 实验 / Prompt 调优回滚 / 紧急下架 |
| **验证** | • 全量 pytest **286/286 PASS**（270 baseline + 16 prompt_loader_version）<br>• 兼容模式：`no_login.yaml` 等 5 个旧 YAML 不修改即可继续走旧路径<br>• Manifest 模式：`agent.yaml` v1/v2 加载 + mtime 热更新验证通过 |
| **Sprint 5 后续阶段** | • 阶段 2：`traffic_ratio` 灰度 + `hash_key` 分配（按需启动）<br>• 阶段 3：按需迁移剩余 5 个 YAML（no_login / query_rewriter/*）<br>• 阶段 4：多租户 prompt 覆盖（Sprint 6 同步）<br>• 阶段 5：DB 存储 + Prompt Editor UI（V3+ 评估） |

---

---

## 4. 优先级与时间投入

### 4.1 阶段划分（与 §2 严重度对齐）

| 阶段         | Sprint                | 时间    | 累计 | 收益                                                                |
|--------------|-----------------------|---------|------|---------------------------------------------------------------------|
| **Phase 1**（P0） | S1 + S2 + S3        | 4.5 周  | 4.5  | 新代码自动走接口 + Prompt 配置化 + 万能模块拆完（再写新代码零技术债） |
| **Phase 2**（P1） | S4 + S5              | 1.5 周  | 6    | 业务规则可灰度 + 文档规范对齐                                        |
| **Phase 3**（P2） | S6                   | 1 周    | 7    | SaaS 化基础就位                                                     |

### 4.2 启动建议

| Sprint | 启动条件                                                  | 前置依赖          |
|--------|-----------------------------------------------------------|-------------------|
| S1     | 无（最先启动）                                            | 无                |
| S2     | 与 S1 并行（Prompt 内容独立于 Provider 抽象）              | 无                |
| S3     | 在 S1 完成后启动（避免两轮跨模块改动叠加）                | S1                |
| S4     | 与 S3 并行                                                | 无                |
| S5     | 任何时候可启动（仅文档）                                  | 无                |
| S6     | Phase 1 全部完成后启动                                    | S1 + S2 + S3      |

### 4.3 并行策略

- S1 + S2 并行（互不依赖）
- S1 完成后 S3 启动
- S4 与 S3 并行（不同模块）
- S5 任意时刻可插入
- S6 必须在 Phase 1 后

**最优时间线：** 7 周（不并行）→ 5 周（按建议并行）

---

## 5. 每个 Sprint 强制遵守的工程纪律（CLAUDE.md V2.1）

| 条款 | 体现                                                                                     |
|------|------------------------------------------------------------------------------------------|
| §4 AI 6 步法 | 每个 Sprint 开工前必走 Step 1-3（任务分析 / 方案 / 等待确认）                            |
| §5 Scope Lock | 默认单模块；S1 涉及 core/providers/llm + core/providers/embedding + core/providers/rerank 是例外（属于"一组接口抽象"，算协同） |
| §5.2 跨模块例外 | S3 拆分 synthesizer 是显式跨模块（4 个新模块 + api/chat），需列 §4.2 四要素              |
| §6 验证分级 | 接口改动 → 单测；Prompt 改 → 黄金用例；DB ALTER → L1 备份回滚；部署 → docker compose config |
| §9.6 Prompt | S2 完成后禁止业务代码再含 prompt 字面量（grep 检查）                                     |
| §9.7 自检 5 问 | 每个 Sprint commit 前必过                                                                  |
| §9.8 八件套 | 每个 Sprint 必交付 README + Protocol + Schema + 测试                                     |
| §3.3 YAGNI | 不引入第二 Provider 实现 / 不引入配置中心 / 不引入热更新机制（除非 Sprint 明确说要）       |
| §9.4.4 L1/L2/L3 | S6 的 9 表 ALTER 是 L1（加可空字段默认值），单 commit + 备份脚本                       |

---

## 6. 暂不做事项（V3+ 再考虑）

| 项                          | 不做的原因                                                                 |
|-----------------------------|----------------------------------------------------------------------------|
| 第二个 AI Provider 实现     | §3.3 YAGNI（当前/近期无第二实现需求）                                       |
| Kafka / MQ 消息总线         | §2 永久禁止（单体架构）                                                    |
| Schema 隔离 / 实例隔离       | V3+ 大客户私有化时再说                                                      |
| Prompt 版本管理 / 灰度 / 热更新 | §3.3 YAGNI；当前 S2 文件版足够                                            |
| 配置中心（Apollo / Nacos）   | §3.3 YAGNI；YAML 文件够用                                                  |
| 动态 RBAC / 权限系统        | §3.3 YAGNI；当前 admin / user 二元足够                                      |
| ADR 全量展开                | 用户明确不要（Phase 0 决议）；仅在产生重大决策时新增 1 篇                   |
| rag/ 顶层迁移               | 用户明确不要（2026-07-11）；仅文档对齐                                       |
| Alembic 迁移系统             | 当前规模不需要；S6 用 SQL 脚本 + Base.metadata.create_all 兜底             |
| 全链路 trace（OpenTelemetry）| 当前阶段 request_id 单服务内 trace 足够                                    |

---

## 7. 启动流程（每个 Sprint 必走）

```markdown
## Sprint 启动文档（docs/decisions/YYYY-MM-DD-sprint-N.md）

### 1. 目标
- 引用 roadmap.md §3.N 的"目标"段

### 2. 范围 vs 不范围
- 引用 + 具体化（文件级）

### 3. 文件清单（变更清单）
- 新建：[路径]
- 修改：[路径:行号]
- 删除：[路径]

### 4. Step 1-2 输出（AI 6 步法）
- 任务分析（涉及模块 / 风险点）
- 方案（接口 / Schema / 测试 / 配置）

### 5. §4.2 跨模块例外（如果触发）
- 业务原因 / 接口变化 / 影响范围 / 隔离策略

### 6. 验证计划（§6 验证分级）
- 单元 + 集成 + curl 路径

### 7. Stop-Loss 8 问 + 自检 5 问
```

每个 Sprint 开工前必须先在 `docs/decisions/` 创建启动文档，**用户确认后**才能执行。

---

## 附录 A：扫描证据（grep 命令清单）

```bash
# 1. 实际目录结构
cd backend && find app -type f -name "*.py" | sort

# 2. Prompt 硬编码搜索
grep -rln "你是一个\|您是" backend/app --include="*.py"
grep -rln "f\"\"\"" backend/app --include="*.py"

# 3. 直接 import 检查（违反 §9.3.3）
grep -rln "from app.core.qwen import" backend/app --include="*.py"
grep -rln "from app.core.embedding import" backend/app --include="*.py"
grep -rln "from app.clients.qdrant import" backend/app --include="*.py"

# 4. tenant_id 缺失
grep -rln "tenant_id" backend/app --include="*.py"   # 0 命中

# 5. config/prompts 缺失
find . -type d -name "prompts"                       # 0 命中

# 6. 万能模块行数
wc -l backend/app/services/synthesizer.py            # 928

# 7. tests 清单
ls backend/tests/
```

## 附录 B：Sprint 时间表

| 周次  | 启动 Sprint                    | 并行 Sprint |
|-------|--------------------------------|-------------|
| W1    | S1（AI 三件套 Provider 抽象）   | S2（Prompt 配置化） |
| W2    | S1 续                          | S2 续 + S5（文档对齐） |
| W3    | S3（synthesizer 拆分）          | S4（业务规则配置化） |
| W4    | S3 续                          | S4 续 |
| W5    | S3 收尾 + S6 启动（多租户）      | —           |
| W6    | S6 续                          | —           |
| W7    | S6 收尾 + 全量回归             | —           |

**关键路径**：S1 → S3 → S6（最长依赖链）

---

## 附录 C：相关文档索引

| 场景                       | 文档                                                  |
|----------------------------|-------------------------------------------------------|
| 业务全景                   | `docs/architecture/business.md`                        |
| 系统形态                   | `docs/architecture/system.md`                         |
| 工程纪律                   | `CLAUDE.md`（V2.1）                                    |
| AI 开发规则               | `docs/governance/ai_development_rules.md`             |
| V1 演进路线（已归档）      | `docs/development/archive/2026-07-11_roadmap_v1_archived.md` |
| 重大架构决策（ADR）       | `docs/decisions/`                                     |
| 学习日志                   | `docs/learning_log.md`                                |

---

> **Roadmap V2 的执行约束（来自用户原话）**：
> - "Roadmap 必须基于实际代码现状，不要基于理想架构重新设计"
> - 严格基于 grep 证据，不动无 grep 命中的代码
> - 不提前迁移 `services/rag/` 到顶层 `rag/`
> - 多租户保持 P2 优先级，不提前