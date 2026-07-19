# 下一阶段可执行计划 V11 — M14 V3 闭环后

> **文档代号**：DEV-V11-NEXT（Next Phase Plan V11）
> **生成方式**：基于 2026-07-19 commit `acf5dd9`（M14 V3 RefundFlow 真实工作流重构 docs）+ 实际代码扫描生成
> **前置基线**：M14 智能客服升级（4 层 + 5 灰度 + 8 件套）+ M14 V3（5 commit）+ V2 验证整改（5 真指标）+ 公网部署 V3 10/10 PASS
> **生成时间**：2026-07-19
> **范围**：仅做分析与规划；不动业务代码；不 git add / commit / push
> **决策权**：用户拍板

---

## 0. 一句话状态

**M14 智能客服升级全 5 阶段 + M14 V3 真实工作流重构（5 commit）已完成；pytest 392/393 PASS；5 灰度开关全 False（M14 = dead branch）；下一阶段有 3 大方向待用户决策**（RAG 优化闭环 / M14 灰度启用 / 业务能力纵深）。

---

## 1. 当前快照（2026-07-19）

### 1.1 关键 commit 矩阵（最近 9 commit）

| commit | 内容 | 类型 | 备注 |
|--------|------|------|------|
| `acf5dd9` | learning_log §48 RefundFlow V3 真实工作流重构 | docs | 最新 |
| `cf28450` | handoff_id 格式断言修复（isupper → hex 字符集）| test | V3 配套 |
| `2f86112` | HandoffCard 三色优先级样式 + P0/P1/P2 字段 | feat(frontend) | V3 配套 |
| `c7c7bde` | chat.py P0 高风险关键词前置拦截 | feat | V3 配套 |
| `d6e4d04` | RefundFlow V3 真实工作流重构（4 节点 + 3 层决策 + Resolver）| feat(m14) | V3 核心 |
| `a08d518` | 公网 10/10 PASS 终态截图 + report.json | chore(verify) | 公网 V3 |
| `76bb854` | 04_products_seed.sql 加 `SET NAMES utf8mb4` 防双重编码 | fix(deploy) | 公网 V3 |
| `ba4ec66` | learning_log §47 公网部署 V3 闭环 10/10 PASS | docs | 公网 V3 |
| `ff4884a` | 公网部署 V3 — 演示界面 + crypto + 等待时间 | fix(m14) | 公网 V3 |

### 1.2 关键指标

| 指标 | 数值 | 来源 |
|------|------|------|
| M14 V3 单测 | 101/101 PASS | 8 测试文件 |
| Backend 全量 pytest | 392/393 PASS | 1 个 MySQL-only pre-existing flake |
| 公网演示（http://120.79.27.124:5173）| 10/10 PASS | M14 公网 V3 |
| 真实话术 5 真指标 | 决策 48% / 分支 60% / 工具 100% / 真幻觉 28% / 政策 15% | run_validation.py V2 |
| 灰度开关状态 | 5 个全 False（M14 = dead branch）| current_status §0 |

### 1.3 ⚠️ 发现 1 项潜在遗漏

**`tests/services/` 下有 2 个 untracked 文件**（git status 显示）：

```
?? tests/services/test_decide_node.py        (19 KB · ~12 case · M14 V3 decide 节点单测)
?? tests/services/test_escalation_categories.py (10 KB · ~12 case · escalation_service 扩展单测)
```

**决策点**：是否立即归档为 commit #9（推荐 · 单 commit · 10min · §5 Scope Lock 合规）

---

## 2. 已落地的能力总览

### 2.1 Sprint / Phase 闭环（按时间倒序）

| Sprint / Phase | 主题 | commit 数 | 关闭缺口 |
|----------------|------|----------|----------|
| M14 V3（最新）| RefundFlow 真实工作流重构 | 5 | V3 业务闭环 |
| M14 公网 V3 | 公网 10/10 PASS | 3 | 公网演示就位 |
| M14 V2 验证整改 | 4 伪指标 → 5 真指标 | 2 | 评测体系升级 |
| **M14 全 5 阶段** | Context + Resolver + SSE Card + BusinessFlow + Audit | 4 | §9.5 可观测从 🟡 → 🟢 |
| B1 + C1+C2+C3 | RAG 评测 + Agent FC 框架 | 7 | Agent FC 闭环 + 评测 |
| **P2 SSE resume** | checkpoint 重发 + 静默 resume + AI 感知 5/5 PASS | 3 | 拟人度 KPI |
| **Sprint 5-1** | Prompt 版本管理（manifest + 兼容）| 2 | §9.6 Prompt 版本 |
| **P2 长程记忆** | user_profiles + profile_service | 3 | 跨 session 记忆 |
| **Phase 4 A4/A5/A8** | Multi-Query + 并行 + 融合后 rerank | 3 | RAG 召回增强 + 性能 |
| Sprint 1-4 + 收尾 | Provider 抽象 + Prompt 架子 + Synthesizer 拆分 + 业务规则 YAML 化 | 18 | G1-G8 全闭环 |

**累计**：47 commit · pytest 392 PASS · 架构验收 🟢 9 / 🟡 3 / 🔴 0

### 2.2 P0/P1 RAG 缺陷修复（9-commit 计划进度 2/9）

| commit | 主题 | 状态 |
|--------|------|------|
| ✅ P1-1 | chunk_id 基于内容 hash 稳定化 + 迁移脚本 | 已完成 · `ingest.compute_chunk_id` + `scripts/migrate_chunk_id.py` + L1/L2 测试 |
| ✅ P1-3 | MySQL 失败回滚 Qdrant | 已完成 · `ingest.ingest_text` rollback 块 + L1/L2 测试 |
| ⏳ P1-2 | BM25 同步重建（异步）| 待启动（task #5 pending）|
| ⏳ P3-3 | RRF 类型加权 | 待启动（task #5 pending）|
| ⏳ P2-1 | 语义切片 | 待启动 |
| ⏳ P2-2 | 细粒度元数据 + knowledge_chunks 表 | 待启动 |
| ⏳ P3-1 | tag 过滤 | 待启动 |
| ⏳ P3-2 | 召回结果标准化 | 待启动 |
| ⏳ P4-1 | relevance 过滤（默认 OFF）| 待启动 |
| ⏳ P4-2 | 引用校验 | 待启动 |
| ⏳ P4-3 | 生成 Prompt 模板 | 待启动 |
| ⏳ P4-4 | 链路日志 | 待启动 |

### 2.3 后台 backlog（current_status §10）

| 序 | 项 | 来源 | 状态 |
|----|-----|------|------|
| 1 | tests/services/ 2 测试 commit 归档 | M14 V3 配套 | ⏳ untracked |
| 2 | M14 5 灰度开关启用 + 真实流量采集 | M14 闭环后 | ⏳ 待启用 |
| 3 | C4 eval_agent_fc.py live | C2 配套 | ⏳ 待跑 |
| 4 | Phase 4 A6 RRF 加权 / A7 HyDE | Phase 4 剩余 | ⏳ 待启动 |
| 5 | V14.x 第 2 个 BusinessFlow | M14 演进 | ⏳ YAGNI |
| 6 | HTTPS（生产部署前置）| P2 backlog 末项 | ⏳ 需外部资源 |
| 7 | Sprint 5-2（traffic_ratio 灰度 + 5 YAML 迁移）| Sprint 5 后续 | ⏳ 按需启动 |
| 8 | Sprint 6 多租户 MVP 预备 | Roadmap §3.7 | ⏳ 待启动 |
| 9 | OrderCard `detailError` UX 优化 | M10 遗留 | ⏳ UX |
| 10 | 用户侧：M14 release notes / healthcheck.io UUID / 简历 baseline | I 项 | ⏳ 用户操作 |

---

## 3. 推荐优先级（4 档 · 含依赖链）

### 🔴 P0（必须本周内 · 收尾+启动）

| # | 任务 | 价值 | 依赖 | 估时 | 验证 |
|---|------|------|------|------|------|
| **P0-1** | tests/services/ 2 untracked 测试 commit #9 归档 | M14 V3 闭环最后一块；清理工作树 | 无 | 10min | pytest 394/395 PASS（+2）|
| **P0-2** | 启动 P1-2 + P3-3（9-commit 计划第 3 commit）| BM25 懒加载首请求 1-3s RT spike → 后台异步；RRF 加权提 hit@K | P1-1/P1-3 已就位 | 半天 | L1 mock + L2 FakeQdrant + §4.2 跨模块 4 要素 |

### 🟠 P1（2 周内 · 验证数据驱动）

| # | 任务 | 价值 | 依赖 | 估时 | 验证 |
|---|------|------|------|------|------|
| **P1-1** | P2 入库层（语义切片 + 细粒度元数据 + knowledge_chunks 表）| 解决"按字符切"破坏语义；支持按 doc_type/tag 召回 | P1-2 完成 | 1 周 | L1 mock + L2 FakeQdrant + DB 断言；§9.4.4 L1 备份回滚 |
| **P1-2** | P3 召回层（tag 过滤 + 标准化返回 + 类型加权生效）| 召回精度提升；hit@K 可量化 | P1-1 + P3-3 | 3 天 | `scripts/eval_hitk.py` 真流量对比 baseline ≥ +10% |
| **P1-3** | M14 灰度 F1-F5 启用（按数据采集需求）| M14 闭环的产品上线必经路径 | 真实流量接入 | 持续 | 4 指标 ≥ 阈值（90% / 95% / 100% / 95%）|

### 🟡 P2（4 周内 · 业务能力纵深）

| # | 任务 | 价值 | 依赖 | 估时 | 验证 |
|---|------|------|------|------|------|
| **P2-1** | P4 生成质量（relevance 过滤默认 OFF + 引用校验 + Prompt + 链路日志）| LLM 反幻觉 + 答案可追溯 | P3 完成 | 1 周 | `scripts/eval_faithfulness.py` ≥ baseline × 1.15 |
| **P2-2** | Phase 4 A6 RRF 加权（如果 P3-3 不够）| 加权多路变体（业务可信度）| P3-3 已就位 | 2 天 | eval_hitk 对比 |
| **P2-3** | V14.x 第 2 个 BusinessFlow（LogisticsFlow / AfterSaleFlow）抽 Base | 当前 1 Flow → 2 Flow → 抽 Base | 业务真实出现 | 1 周 | 灰度开关 default false + 8 件套 |
| **P2-4** | C4 eval_agent_fc.py live + ENABLE_AGENT_FC 灰度 | Agent FC 框架就位后启用 | C4 live ≥ 0.7 阈值 | 3 天 | tool_selection_accuracy ≥ 0.7 |

### 🟢 P3（按需启动 · 不抢资源）

| # | 任务 | 价值 | 备注 |
|---|------|------|------|
| **P3-1** | Sprint 5-2（traffic_ratio 灰度 + 5 YAML 迁移） | PM 视角灰度 | MVP 用户拍板暂缓 |
| **P3-2** | Sprint 6 多租户 MVP 预备（9 表加 tenant_id） | SaaS 化基础 | §9.4.3 长期演进 |
| **P3-3** | HTTPS（生产部署前置）| 生产环境必需 | 需外部资源（域名 + certbot）|
| **P3-4** | OrderCard `detailError` UX 优化 | UX 小瑕疵 | M10 遗留 |
| **P3-5** | Phase 4 A7 HyDE（生成假设性答案作 embedding query）| 高级 RAG 召回 | A5+A8+P3-3 不够再考虑 |

---

## 4. 推荐路线（3 选 1）

### 路径 A：RAG 优化闭环（推完 9-commit 计划） ★ 推荐

```
本周    P0-1（commit #9 归档） → P0-2（启动 P1-2 + P3-3）
下周    P1-1（P2 入库层 1 周）
第3-4周 P1-2（P3 召回层 3 天）+ P2-1（P4 生成质量 1 周）并行
────────────────────────────────────────────────
总计    约 3 周；7-10 commit；RAG 全栈优化闭环
```

**理由**：
- 9-commit 计划已启动 2/9，剩余在逻辑上递进
- 每个 commit 都有 §9.8 8 件套 + SOP-V1 §2.2 四要素 + §4.2 跨模块四要素基础
- 灰度门禁默认 false，对外行为零漂移（CLAUDE.md §9.4.4 fail-fast 测试就位）
- 当前产品 demo 体验提升的最大杠杆（hit@K + 反幻觉 + 引用溯源）

### 路径 B：M14 灰度启用（产品上线必经）

```
本周    P0-1（commit #9 归档） → M14 F1（ENABLE_CONTEXT_STORE=true）
下周    M14 F2（ENABLE_ORDER_RESOLVER=true）→ F3（SSE_CARD_V2 已默认 true）
第3周   M14 F4（ENABLE_BUSINESS_FLOW=true）→ F5（ENABLE_ESCALATION_HANDOFF=true）
────────────────────────────────────────────────
总计    约 2 周；灰度风险高；需真实流量基础
```

**理由**：M14 闭环后的核心目标，但**强烈依赖真实流量基础**。当前 demo 流量不足以采集 F1-F5 阈值数据。

### 路径 C：业务能力纵深（与 M14 并行可做）

```
P2-2（A6 RRF 加权） + P2-3（V14.x 第 2 Flow） + P2-4（C4 live + Agent FC 灰度）
```

**理由**：M14 闭环后，产品需要"第二个 Flow"出现才能抽 Base 类（§3.3 YAGNI 触发条件）；Agent FC 灰度决策质量已通过 C4 0.7 阈值验证。

### 路径 D：A + C 并行（先 P0-1/P0-2/P1-1 → M14 灰度准备）

```
3 周 RAG 优化 + M14 灰度预演（在 P0 阶段先跑 tests/eval 验证 4 指标）
────────────────────────────────────────────────
总计    约 4 周；价值最大；风险中等
```

---

## 5. 9-commit RAG 优化计划剩余 10 commit 详情

| commit | 主题 | 范围 | 验证 | 估时 |
|--------|------|------|------|------|
| ✅ P1-1 | chunk_id 稳定化 | `ingest.compute_chunk_id` | L1 + L2 | 已完成 |
| ✅ P1-3 | MySQL 失败回滚 Qdrant | `ingest.ingest_text` rollback | L1 + L2 | 已完成 |
| **P1-2** | BM25 同步重建（异步）| `bm25_index.invalidate_and_rebuild_async` + ingest 触发 | L1 + L2 + ThreadPoolExecutor | 0.5 天 |
| **P3-3** | RRF 类型加权 | `rrf_fuse(weights=)` + `policy_service` 透传 | L1 + L2 | 0.5 天 |
| **P2-1** | 语义切片 | `rag/ingest.semantic_chunk_text` + jieba fallback | L1 + L2 | 2 天 |
| **P2-2** | 细粒度元数据 + knowledge_chunks 表 | models + migration + ingest | L1 + L2 + DB 断言 | 2 天 |
| **P3-1** | tag 过滤 | `pipeline.prefilter_by_tag` + `settings.RAG_TAG_FILTER` | L1 + L2 | 1 天 |
| **P3-2** | 召回结果标准化 | `pipeline.format_recall_result` Pydantic | L1 | 1 天 |
| **P4-1** | relevance 过滤（默认 OFF）| `policy_service.filter_by_relevance` + score threshold | L1 + eval | 1 天 |
| **P4-2** | 引用校验 | `chat/orchestrator` verify citations | L1 + L2 | 1 天 |
| **P4-3** | 生成 Prompt 模板 | `config/prompts/rag/{system,user}.yaml` | L1 + 黄金用例 | 1 天 |
| **P4-4** | 链路日志 | `metrics.inc_rag_latency` + structured log | L1 | 0.5 天 |

**累计**：剩 10 commit（之前预估 7 但展开后 10），估时约 12-15 天 ≈ 3 周工作量。

---

## 6. 待确认决策（4 项）

### 决策 1：核心方向

| 选项 | 含义 | 工作量 | 风险 | 价值 |
|------|------|--------|------|------|
| **A** | 推完 9-commit RAG 优化（路径 A） | 3 周 | 低（灰度默认 false）| 中-高 |
| **B** | M14 灰度启用（路径 B） | 2 周 | 中-高（需真实流量）| 高（产品上线）|
| **C** | 业务能力纵深（路径 C） | 2-3 周 | 低（YAGNI 边界清晰）| 中 |
| **D** | A + C 并行（先 P0-1/P0-2 + P1-1 → M14 灰度准备） | 4 周 | 中 | 高 |

### 决策 2：P1-2 + P3-3 是否继续

之前 task #5 pending。是否推完整个 RAG 优化？

| 选项 | 含义 |
|------|------|
| 推完 | 3 周；10 commit 全闭环；hit@K 显著提升 |
| 仅 P1-2 + P3-3 | 1 天；保留 §9.8 灰度门禁可关 |
| 跳过 | focus M14 灰度 / Agent FC 灰度 |

### 决策 3：tests/services/ 归档策略

| 选项 | 含义 |
|------|------|
| **A. 立即归档**（推荐）| 单 commit #9；清理工作树；M14 V3 闭环的最后一笔 |
| B. 等下一阶段 commit 一起归档 | 延迟但符合 §5 Scope Lock "改 A 不带 B" |
| C. 删掉这 2 个文件 | ❌ 不推荐（V3 测试覆盖丢失）|

### 决策 4：M14 灰度启用时机

| 选项 | 含义 |
|------|------|
| A. 本周启动 F1（最稳：ContextService 读多写少）| 1 周；逐步放量 |
| B. 等真实流量峰值时一次性开 4 灰度 | 风险高；省时间 |
| C. 暂缓（M14 代码保持 dead branch）| 风险最低；机会成本高 |
| D. 启动前先跑 `tests/eval/test_m14_resolver_metrics.py` 4 指标预演 | 1 天；如果指标 ≥ 阈值则开 |

---

## 7. 风险与护栏

| 风险 | 概率 | 影响 | 护栏 |
|------|------|------|------|
| **P0-1 untracked 测试归档混入业务改动** | 低 | 高（违反 §5 Scope Lock）| 单独 commit #9；不改任何业务文件 |
| **P1-2 异步重建泄漏线程** | 中 | 中 | per-request executor pattern（A5 经验）；`with` 块生命周期对齐请求 |
| **P3-3 RRF 加权引入 0 权重导致除零** | 低 | 中 | L1 测试覆盖权重=0 / 权重=1 / 缺省权重 三态 |
| **P2-2 新表 knowledge_chunks 破坏既有 schema** | 低 | 高 | §9.4.4 L1 备份回滚；不改 knowledge_documents 表 |
| **P4-1 relevance 过滤默认 OFF 不生效** | 中 | 中 | 测试断言 `default OFF` + `eval_faithfulness.py` 真流量 baseline 对比 |
| **M14 灰度启用数据不足** | 高 | 中 | F1 → F5 灰度递进；每步停 24h 看指标 |

---

## 8. 验证清单（每类任务必走）

| 任务类型 | 验证手段 | 通过判据 |
|----------|----------|----------|
| 配置开关新增 | L1 mock + 灰度回退测试 | default false + 关闭后行为不变 |
| BM25 异步重建 | L1 mock + L2 FakeQdrant + ThreadPoolExecutor 验证 | 异步不阻塞主流程；invalidate 后下次 search 重建 |
| RRF 加权 | L1 mock + L2 召回对比 | 加权=1 时退化为标准 RRF；加权=0 时该 doc 不出现 |
| 数据库变更（L1）| 迁移脚本 + DB 断言 + pytest | 数据无丢失 + 新表 schema 正确 |
| 生成质量 | eval_faithfulness 真流量对比 | hit@K ≥ baseline × 1.15 + hallucination ≤ baseline × 0.5 |
| M14 灰度 | tests/eval/test_m14_resolver_metrics.py | 4 指标 ≥ 阈值（90% / 95% / 100% / 95%）|
| 链路日志 | L1 + caplog 验证结构化字段 | 每次 RAG 调用一行 `[rag_metrics] ...` |

---

## 9. 不在下一阶段范围（YAGNI · §3.3）

| 不做的项 | 原因 |
|----------|------|
| 第二个 AI Provider 实现 | 当前/近期无第二实现需求 |
| Kafka / MQ 消息总线 | §2 永久禁止（单体架构）|
| Schema 隔离 / 实例隔离 | V3+ 大客户私有化时再说 |
| Prompt 版本管理 rollout 灰度 | MVP 用户拍板暂缓 |
| 配置中心（Apollo / Nacos）| YAML 文件够用 |
| 动态 RBAC / 权限系统 | admin / user 二元足够 |
| Rag/ 顶层迁移 | 用户明确不要 |
| Alembic 迁移系统 | 当前规模不需要 |
| 全链路 trace（OpenTelemetry）| request_id 单服务内 trace 足够 |

---

## 10. 一句话恢复提示

```
当前快照：M14 V3 真实工作流重构完成（commit acf5dd9 · 392 PASS）
         + P1-1/P1-3 已落地 · tests/services/ 2 测试 untracked 待归档

下一步决策（待用户拍板）：
  D1: 核心方向 A/B/C/D（RAG 优化 / M14 灰度 / 业务纵深 / 并行）
  D2: P1-2 + P3-3 是否继续（task #5）
  D3: tests/services/ 归档时机
  D4: M14 灰度启用时机

待启动前必读 7 件套：
  CLAUDE.md V2.1 · docs/development/current_status.md v10.0
  docs/development/roadmap.md §3.5.1+§3.8-§3.14
  docs/architecture/m14_module.md · docs/learning_log.md §28-§48
  docs/governance/ai_development_sop.md SOP-V1

本次会话：仅分析与规划；未修改业务代码；未 commit / push
```

---

## 11. 推荐顺序（基于"先稳后扩"原则）

| 序 | 动作 | 理由 |
|----|------|------|
| **1** | **本周先 P0-1（commit #9 归档）+ P0-2（启动 P1-2 + P3-3）** | 收尾 M14 V3 闭环 + RAG 优化进度延续 2/9 |
| **2** | **下周启动 P1-1（P2 入库层 · 语义切片 + knowledge_chunks 表）** | RAG 全栈优化的最大杠杆（切片质量决定召回天花板）|
| **3** | **第 3-4 周并行做 P1-2（P3 召回层）+ P2-1（P4 生成质量）** | 召回 + 生成是 RAG 闭环后半段 |
| **4** | **M14 灰度启动在 P0-1 完成 + 真实流量稳定后** | 不要在 RAG 优化期间叠加灰度风险 |
| **5** | **HTTPS / Sprint 6 多租户 / Phase 4 A7 HyDE** 放后续 sprint | 当前阶段非关键路径 |

---

## 12. 新增：可观测性增强 P4（对话审计 + admin 全局查询 + 运营聚合）

> **触发**：用户 2026-07-19 提问「对话数据会进 sql 吗，可以历史查询吗」触发调研；
> 发现 4 个缺口，与原计划 v11 §3-§11 不重叠，作为 **P4 新阶段** 并行候选。

### 12.1 当前现状（已实现部分）

| 已落地 | 来源 |
|--------|------|
| `conversations` + `messages` 双表持久化（messages.contexts JSON 含 RAG 召回原文）| `models/conversation.py` / `models/message.py` |
| 4 个用户级 API（list / get / delete / patch title），全部 RBAC 校验归属 | `api/conversations.py` |
| 软删（deleted=1 保留审计）+ Redis 缓存清理 + audit log | `conversations.py:308` |
| `messages.contexts/scores/token_count/latency_ms` 全字段入库 | `models/message.py:18-22` |

### 12.2 4 个候选缺口

| ID | 缺口 | 价值 | 工时 | 优先级 |
|----|------|------|------|--------|
| **P4-1** | admin 全局会话查询 API（跨用户审计 + 按 user_id / 时间 / 关键词过滤）| ⭐⭐⭐⭐⭐ 简历 RBAC + 运营审计必问 | 1.5-2h | **P0 推荐先做** |
| **P4-2** | 运营聚合指标 API（7 日响应延迟 P50/P95 / 触发 escalation 的 query 类型分布 / hit@K 自统计）| ⭐⭐⭐⭐ 简历「可观测性」必问 | 2-3h | P1 |
| **P4-3** | 单对话 export + replay JSON（重放某次对话复现 bug）| ⭐⭐⭐ 调试效率翻倍 | 1h | P2 |
| **P4-4** | `messages.contexts` JSON 索引（M3 生产规模才需要）| ⭐ MVP 不急 | 30min | P3 |

### 12.3 P4-1 推荐设计（admin 全局会话查询）

#### 路由

```
GET    /api/admin/conversations                        # 全局会话列表（按时间倒序）
       ?user_id=123                                     # 过滤用户
       &start_date=2026-07-01&end_date=2026-07-19       # 时间窗
       &keyword=退款                                     # 全文搜索 first_query / messages.content
       &has_handoff=true                                # 仅含转人工的会话
       &min_latency_ms=3000                             # 慢查询
       &limit=50&cursor=xxx                             # cursor 分页
GET    /api/admin/conversations/{sid}/messages         # 全局消息（任意 sid 可读）
GET    /api/admin/conversations/analytics               # 聚合指标
```

#### RBAC

| 角色 | 权限 |
|------|------|
| `user` | 只能看自己的会话（现状不变）|
| `admin` | 全局只读，含 user_id 字段脱敏（手机号/邮箱 → ***） |
| `super_admin` | 全局读写 + 可触发 replay |

#### 性能与边界

| 关注 | 方案 |
|------|------|
| 全表扫描风险 | 强制 `start_date` + `end_date`（无日期窗返 400）|
| 全文搜索性能 | MVP 用 LIKE '%keyword%'；P4-2 之后考虑 ES / MySQL FULLTEXT |
| 数据量 | 1 万条/天 × 30 天 = 30 万条；SELECT + LIMIT 50 < 100ms |
| 审计 | 所有 admin 查询走 `audit_logs` 表（已有 `try_log_action` 复用）|

#### 与现有约束对齐

| 约束 | 满足方式 |
|------|---------|
| CLAUDE.md §9.2.2 禁止跨模块侵入 | 新建 `api/admin_conversations.py`，不修改 `api/conversations.py` |
| §9.3.1 5 件套 | Type Hints + Pydantic Response + 自定义异常 + 业务码 + DTO |
| §9.4.4 L1 数据库 | 仅新建查询索引（user_id / last_message_at），无破坏性 |
| §9.5.2 可观测性 | 所有 admin 查询记录 request_id + admin_id + 命中条数 |
| §9.7 自检 5 问 | Protocol 在 `api/admin_conversations/protocols.py` 就近 |

#### 测试

- L1 mock：5 case（RBAC / 时间窗 / 关键词 / 分页 / 脱敏）
- L2 集成：用 `db_session` fixture 造 100 条会话，验证 LIMIT + cursor
- E2E：playwright admin 登录 → 查某用户会话 → 截图

### 12.4 实施顺序（推荐）

```
本周 P0-2 收尾（已完成）
    ↓
下周启动 P4-1 admin 全局查询（推荐先做，价值最高，工时最短）
    ↓
再下周 P4-2 运营聚合（依赖 P4-1 的基础设施）
    ↓
按需 P4-3 export/replay（看调试痛不痛再决定）
    ↓
P4-4 索引（M3 生产规模才需要，暂搁置）
```

### 12.5 与原计划 v11 §11 推荐顺序的关系

| 维度 | 原 v11 §11 | 新增 v11 §12 |
|------|-----------|-------------|
| 焦点 | RAG 优化（召回 + 切片 + rerank）| 可观测性（admin + 聚合 + 调试）|
| 简历加分 | 算法能力（RRF/BM25/HyDE）| 工程能力（RBAC + 审计 + 可观测性）|
| 业务价值 | 提升 AI 回答质量 | 降低运营/调试成本 |
| 投入产出 | 中（需真实数据验证 hit@K 提升）| 高（功能明确，admin 全局查询一次写完即用）|

**两者并行不冲突**：P4 改 api 层 + 模型层（不碰 rag/），原 P1/P3 改 rag 层（不碰 api/）。

---

## 附录 A：变更日志

| 版本 | 日期 | 变更 |
|------|------|------|
| V11 | 2026-07-19 | 初版 · 基于 M14 V3 commit `acf5dd9` 状态生成 |
| V11.1 | 2026-07-19 | 新增 §12 可观测性增强 P4 · 4 个缺口调研 + P4-1 admin 全局查询设计 |

---

**文件版本**：V11 · 2026-07-19 · M14 V3 闭环后规划
**下次更新**：用户对 4 项决策拍板后（轻量修订）/ 进入执行后（重写为 Sprint 启动文档）