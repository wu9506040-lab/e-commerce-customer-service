# 项目当前状态 · 恢复记忆（2026-07-12 暂停点）

> 本文件是**会话级恢复记忆**，不是长期文档基线。
> 长期基线：`CLAUDE.md`（V2.1） / `docs/architecture/business.md` / `docs/development/roadmap.md`。
> 本文件下次启动开发时**先读**，然后决定是否仍相关（轻量修订即可）。

---

## 0. 一句话状态

**Phase 0 治理 + Sprint 1 AI Provider 抽象 + Sprint 2 Prompt 基础设施全部完成；进入 Sprint 3 待启动。**

8 个 commit 已提交（Sprint 1: 24fed9a / 674ac50 / 54a4b52 / afd6b50；Sprint 2: 1f705fc / 910663c / 05d5965 / 68d5700），全量 pytest 150 passed，工作树干净（仅 Sprint 1 范围外的旧修改与新临时文件残留）。

---

## 1. 当前项目阶段

| 阶段 | 状态 | 备注 |
|------|------|------|
| Phase 0 治理（CLAUDE.md V2.1 + docs/ 子目录化） | ✅ 完成 | docs/README.md / business.md / governance/ai_development_rules.md 仍 untracked（待单独收尾 commit） |
| Sprint 1（AI Provider 抽象） | ✅ 完成 | G1/G2/G10 三个 Roadmap 缺口关闭 |
| Sprint 2（Prompt 基础设施） | ✅ 完成 | **G6** 缺口关闭；G5 部分关闭（架子就位；业务抽取由 S3 完成） |
| Sprint 3（Synthesizer 拆分） | ⏸ 待启动 | 下一次开发入口 |

**Roadmap V2**（`docs/development/roadmap.md`）共 6 个 Sprint，目前完成 1/6。

---

## 2. 已完成的 Sprint / 任务

### Phase 0（已完成，未 commit 全部子目录文件）
- CLAUDE.md V2.1（精简 + 6 步法 + 13 反例 + 架构要求）
- docs/ 子目录化（architecture / governance / development / decisions）
- Roadmap V1 → V2（基于实际代码扫描；roadmap_v1 已归档）

### Sprint 1：AI 三件套 Provider 抽象（4 个 commit）

| commit | 类型 | 内容 | 文件数 |
|--------|------|------|--------|
| `24fed9a` | feat(core) | 新增 Provider 抽象层 | 10 |
| `674ac50` | refactor(services) | 业务切到 Provider（13 调用点） | 13 |
| `54a4b52` | test(core) | Provider 契约测试 + mock 同步 | 5 |
| `afd6b50` | chore(core) | legacy qwen/embedding 标 deprecated + 文档同步 | 4 |

---

## 3. 本次会话实际修改的文件清单

### 新建（10）
```
backend/app/core/providers/__init__.py
backend/app/core/providers/llm/{__init__.py, protocols.py, qwen_provider.py}
backend/app/core/providers/embedding/{__init__.py, protocols.py, qwen_provider.py}
backend/app/core/providers/rerank/{__init__.py, protocols.py, qwen_provider.py}
backend/tests/test_provider_protocols.py
```

### 改造业务调用（13）
```
backend/app/services/synthesizer.py        # LLM stream_chat
backend/app/services/intent_service.py     # LLM chat
backend/app/services/query_rewriter.py     # LLM chat
backend/app/services/refund_graph.py       # LLM chat
backend/app/services/rag/pipeline.py       # LLM stream_chat + Embedding embed_text
backend/app/services/rag/ingest.py         # Embedding embed_texts
backend/app/services/rag/test_pipeline.py  # Embedding embed_texts
backend/app/services/policy_service.py     # Embedding embed_text（懒加载）
backend/app/services/guard.py              # Embedding embed_text + EmbeddingError
backend/app/services/guard_centroid.py     # Embedding embed_texts + EMBEDDING_DIM → provider.get_dim()
backend/app/services/response_cache.py     # Embedding embed_text + EmbeddingError
backend/app/services/bm25_index.py         # 清理 dead import
backend/app/services/rerank.py             # 改为薄垫片，业务迁入 core/providers/rerank
```

### 测试 mock 同步（4）
```
backend/tests/test_anti_hallucination.py    # patch qwen_stream_chat → patch get_llm_provider
backend/tests/test_refund_graph.py          # patch qwen_chat → patch get_llm_provider
backend/tests/test_source_attribution.py    # 同上
backend/tests/test_synthesizer_refund.py    # 同上
```

### Deprecated 注释 + docstring 修复（2）
```
backend/app/core/qwen.py                   # 顶部加 ⚠️ DEPRECATED 注释
backend/app/core/embedding.py              # 顶部加 ⚠️ DEPRECATED 注释 + 修复 M7 commit 留下的 docstring 截断 bug
```

### 文档同步（3，已 commit）
```
docs/development/roadmap.md                # S1 涉及路径 core/llm → core/providers
docs/architecture/system.md                # RAG + Agent 行路径同步
CLAUDE.md §9.9                             # 路径表同步（CLAUDE.md 本地保留，未 commit）
```

---

## 4. 重要架构变化（影响后续开发）

### 4.1 新依赖方向（单向）
```
services/  ──→  core/providers/{llm,embedding,rerank}/
                              │
                              └──→  core/qwen.py / core/embedding.py（deprecated 垫片）
```
业务模块**禁止**直接 `from app.core.qwen import` 或 `from app.core.embedding import`。

### 4.2 Provider 接口（最小化，YAGNI 已落实）
| Provider | 方法 |
|----------|------|
| `LLMProvider` | `chat()` / `stream_chat()` |
| `EmbeddingProvider` | `embed_text()` / `embed_texts()` / `get_dim()` / `get_model()` |
| `RerankProvider` | `rerank()` / `rerank_async()` |

**未引入**（故意保持最小）：`health_check` / `switch_model` / `fallback` / `metrics` / `cost` / `retry` / `breaker`。V2+ 真实需要时再加。

### 4.3 单文件规模（全部 < 200 行目标）
最大：`rerank/qwen_provider.py` = 199 行（业务逻辑完整保留，未拆分）
其余：< 60 行

### 4.4 删除计划（重要）
- `core/qwen.py` / `core/embedding.py` / `services/rerank.py` 删除计划 = **S4 末**
- S2 / S3 期间必须保留（防止回归）

---

## 5. 测试与验证结果

| 项 | 结果 |
|----|------|
| 全量 pytest | **129 passed**（含 14 个新 Provider 契约测试） |
| 调用点 grep | `grep "from app.core.qwen\|from app.core.embedding" backend/app/services/` → **0 命中** |
| 反向依赖 grep | `grep "from app.services" backend/app/core/providers/` → **0 命中** |
| Smoke test | 3 个 Provider 工厂返回正确类型，13 个业务模块 import 全部 OK |
| Step 6 AI Review | 5 项检查全部通过（解耦 / 反向依赖 / Protocol 最小化 / Factory 简洁 / 单文件规模） |

**测试启动方式**：
```bash
JWT_SECRET="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" \
DATABASE_URL="mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4" \
  python -m pytest tests/
```

---

## 6. 当前 Roadmap 进度（Roadmap V2）

| Sprint | 主题 | 优先级 | 关闭缺口 | 状态 |
|--------|------|--------|----------|------|
| S1 | AI 三件套 Provider 抽象 | 🔴 P0 | G1/G2/G10 | ✅ 完成 |
| S2 | Prompt 基础设施（loader + 目录） | 🔴 P0 | G6 | ✅ 完成 |
| S3 | Synthesizer 拆分（928 → 4 模块） | 🔴 P0 | G5（主要）降 G7 | ⏸ 待启动 |
| S4 | 业务规则配置化（阈值 YAML 化） | 🟠 P1 | G8 | ⏸ 待办 |
| S5 | 目录对齐 CLAUDE.md §7.1（仅文档） | 🟢 P2 | G11-G13 | ⏸ 待办 |
| S6 | 多租户 MVP 预备 | 🟢 P2 | G9 | ⏸ 待办 |

Roadmap V1 已归档：`docs/development/archive/2026-07-11_roadmap_v1_archived.md`

---

## 7. 下一次恢复开发的入口

### 7.1 启动 Sprint 3 之前必读
1. `docs/development/roadmap.md` §S3（确认 G5/G7 范围）
2. `docs/decisions/2026-07-12-sprint-2-prompt-loader.md`（Sprint 2 ADR + S3 启动前置）
3. `docs/governance/ai_development_rules.md`（AI 开发规则）
4. 本文件 §4（确认 Provider 抽象约束未被破坏）

### 7.2 Sprint 3 任务模板（Synthesizer 拆分）
- **Step 1 任务分析**：列出涉及模块（synthesizer.py → 4 个新模块 + api/chat.py + 5 个 prompt YAML）
- **Step 2 方案设计**：拆分边界图（orchestrator / prompt_assembler / citation_formatter / stream_dispatcher）
- **Step 3 等待确认**：跨模块改动按 §4.2 列四要素（业务原因 / 接口变化 / 影响范围 / 隔离策略）
- **Step 4 开发**：参考 Sprint 1 模式（Protocol 优先 → 实现 → 工厂入口 → 切换调用方 → 测试）
- **Step 5 提交归档**：建议按 Sprint 1/2 同样 4 commit 节奏
- **Step 6 AI Review**：同 Sprint 1 五项检查单

### 7.3 Sprint 2 启动时环境准备
- 工作树当前 M 文件：`scripts/eval_hitk.py`（非 Sprint 1 修改，可能是其他任务遗留，**不要混入 Sprint 2 commit**）
- 工作树当前 ?? 文件：`docs/README.md` / `docs/architecture/business.md` / `docs/governance/` / `chat_*.json` / `*.py` 临时文件 → 启动前评估是否清理

---

## 8. 未完成任务

### 8.1 Sprint 1 范围内：全部完成 ✅
### 8.2 项目整体（不属于 Sprint 1）
| 项 | 性质 | 说明 |
|----|------|------|
| `scripts/eval_hitk.py` 修改未 commit | 旧任务遗留 | 应单独评估归属（可能是 eval hit@K 脚本） |
| `docs/README.md` untracked | Phase 0 收尾 | 内容已写完，可单独 commit "docs: 子目录化首次提交" |
| `docs/architecture/business.md` untracked | Phase 0 收尾 | V3.1 业务架构基线 |
| `docs/governance/ai_development_rules.md` untracked | Phase 0 收尾 | AI 开发规则 |
| `chat_*.json` / `add_terms.py` / `insert_sections.py` / `merge_extra.py` / `restore_prefix.py` / `extra_sections.md` | 临时调试残留 | 启动前判断是否清理或归档 |

### 8.3 Sprint 2-S6
详见 §6 表格。

---

## 9. 当前禁止提前执行的事项

| # | 禁止 | 原因 |
|---|------|------|
| 1 | 删除 `core/qwen.py` / `core/embedding.py` / `services/rerank.py` | 删除计划 S4 末；S2/S3 期间作为兼容垫片保留 |
| 2 | 引入 `VectorStore` Protocol | YAGNI（V3+） |
| 3 | 引入第二个 Provider 实现（GPT / Claude / BGE） | YAGNI；当前 1 个实现 |
| 4 | 往 Provider 加 health_check / switch_model / fallback / metrics / cost / retry / breaker 等扩展方法 | YAGNI；真实需要时再加 |
| 5 | Provider 直接 `new` 具体类（绕过 `get_xxx_provider()`） | 违反依赖倒置；所有调用必须走工厂 |
| 6 | `core/providers/*.py` 反向依赖 `services/` | 破坏 §9.2.3 单向依赖 |
| 7 | 启动 Sprint 3+ | 必须先 Sprint 2 完成且归档 |
| 8 | 把 Sprint 1 范围外的 untracked 文件混入 Sprint 2 commit | 违反 §5 Scope Lock |
| 9 | 把 Provider 改造反向迁移回 `from app.core.qwen import` 旧风格 | Sprint 1 切换成果回滚 |

---

## 10. 需要继续遵守的 CLAUDE.md 规则（高频检查项）

| 条款 | 内容 | 何时触发 |
|------|------|----------|
| §2 禁止行为 | 8 条永久禁止（Kafka / 微服务 / 跨模块 / YAGNI / 硬编码等） | 每次 PR 自检 |
| §4 AI 6 步法 | 任务分析 → 方案 → 确认 → 开发 → 归档 → Review | 每个开发任务强制 |
| §4.2 跨模块例外 | 涉及 ≥ 2 模块必列四要素 | 改动跨模块时 |
| §4.4 Stop-Loss 8 问 | 涉及模块 / 接口签名 / 依赖 / 配置 / 测试 / 五问 / 八件套 / commit 粒度 | 提交前自检 |
| §4.5 AI Review 5 项 | §2 禁止 / 跨模块耦合 / YAGNI / 安全合规 / 接口影响 | 提交前 |
| §5 Scope Lock | 默认单模块；跨模块走 §4.2 | 评估改动范围 |
| §9.1 三大铁律 | Interface First / Module Isolation / Dependency Inversion | 任何接口改动 |
| §9.3.3 AI 能力抽象 | 所有 AI 能力必须 Provider 抽象，业务模块禁直接调第三方 SDK | Sprint 1 已落地 |
| §9.7 改前 5 问 | 跨模块 import / 模块边界 / Protocol 先于实现 / 接口签名 / 单测可独立 | 改 Provider / Service 时 |

---

## 11. 一句话恢复提示

下次启动：
```
读 CLAUDE.md V2.1 → 读 roadmap.md S2 → 读本文件 §4/§9 确认约束
→ 启动 Sprint 2 Step 1 任务分析（Prompt 基础设施）
```

---

## 附录：会话关键数据（grep 一句话验证）

```bash
# 验证 Sprint 1 落地
grep -rn "from app.core.qwen import\|from app.core.embedding import" backend/app/services/
# 期望：0 命中

# 验证反向依赖
grep -rn "from app.services\|import app.services" backend/app/core/providers/
# 期望：0 命中

# 验证 Provider 接口最小化
grep -nE "health_check|switch_model|fallback|metrics|cost" backend/app/core/providers/*/protocols.py
# 期望：0 命中
```

---

**文件版本**：v2 · 2026-07-12 Sprint 2 完成时更新
**下次更新**：Sprint 3 启动前（轻量修订） / Sprint 3 完成时（重写 Sprint 段落）