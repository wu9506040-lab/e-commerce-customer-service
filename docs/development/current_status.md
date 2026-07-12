# 项目当前状态 · 恢复记忆（2026-07-12 Sprint 3 完成时）

> 本文件是**会话级恢复记忆**，不是长期文档基线。
> 长期基线：`CLAUDE.md`（V2.1） / `docs/architecture/business.md` / `docs/development/roadmap.md`。
> 本文件下次启动开发时**先读**，然后决定是否仍相关（轻量修订即可）。

---

## 0. 一句话状态

**Phase 0 治理 + Sprint 1 AI Provider 抽象 + Sprint 2 Prompt 基础设施 + Sprint 3 Synthesizer 拆分全部完成；进入 Sprint 4 待启动。**

13 个 commit 已提交（Sprint 1: 24fed9a / 674ac50 / 54a4b52 / afd6b50；Sprint 2: 1f705fc / 910663c / 05d5965 / 68d5700；Sprint 3: 63e7044 / 7f343fd / a8bea00 / 5ee01f6 / [c5 pending]），全量 pytest 168 passed（新增 18 个纯函数测试），工作树待 commit 5 归档。

---

## 1. 当前项目阶段

| 阶段 | 状态 | 备注 |
|------|------|------|
| Phase 0 治理（CLAUDE.md V2.1 + docs/ 子目录化） | ✅ 完成 | docs/README.md / business.md / governance/ai_development_rules.md 仍 untracked（待单独收尾 commit） |
| Sprint 1（AI Provider 抽象） | ✅ 完成 | G1/G2/G10 三个 Roadmap 缺口关闭 |
| Sprint 2（Prompt 基础设施） | ✅ 完成 | **G6** 缺口关闭；G5 部分关闭（架子就位；业务抽取由 S3 完成） |
| Sprint 3（Synthesizer 拆分 928→5 模块） | ✅ 完成 | **G5 主要关闭 + G7 降级**（928→64 薄壳 + 5 子模块；仅抽 2/5 Prompt，Range A 决议） |

**Roadmap V2**（`docs/development/roadmap.md`）共 6 个 Sprint，目前完成 3/6。

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

### Sprint 2：Prompt 基础设施（4 个 commit）

| commit | 类型 | 内容 | 文件数 |
|--------|------|------|--------|
| `1f705fc` | chore(deps) | 新增 PyYAML==6.0.2 锁版本 | 1 |
| `910663c` | feat(services) | 新增 prompt_loader 统一加载器 | 4 |
| `05d5965` | chore(config) | PROMPT_DIR + config/prompts 架子 + Dockerfile COPY | 3 |
| `68d5700` | test(services) | prompt_loader 单元测试 21 用例 + mtime 顺序 bugfix | 1 |

### Sprint 3：Synthesizer 拆分 928→5 模块（5 个 commit）

| commit | 类型 | 内容 | 文件数 |
|--------|------|------|--------|
| `63e7044` | docs(sprint3) | Sprint 3 启动 ADR（Synthesizer 拆分 + 范围 A 决议） | 1 |
| `7f343fd` | feat(prompts) | 新建 agent + no_login YAML（Sprint 3 抽 2/5 Prompt） | 2 |
| `a8bea00` | feat(chat) | cp 完整 4 个新模块（安全网，不动旧代码） | 6 |
| `5ee01f6` | refactor(synthesizer) | 切换 import + 旧 synthesizer 薄壳化 + refund_v2/v3 移出 | 9 |
| `[c5]` | test+docs | 新增 18 个纯函数单测 + 文档收尾（roadmap S3 ✅ + 本文件 v3 + learning_log §27） | 5 |

---

## 3. Sprint 3 本次会话实际修改的文件清单

### 新建 6（chat/ 子包）
```
backend/app/services/chat/__init__.py                  # 空
backend/app/services/chat/orchestrator.py              # Synthesizer 主类
backend/app/services/chat/prompt_assembler.py          # 7 个纯字符串拼接函数
backend/app/services/chat/stream_dispatcher.py         # stream_llm + 滑窗检索
backend/app/services/chat/refund_handler.py            # handle_refund_v2/v3
backend/app/services/chat/citation_formatter.py        # 占位（未来扩展位）
```

### 新建 2（Prompt YAML · Range A）
```
backend/config/prompts/agent.yaml                      # SYSTEM_PROMPT_BASE
backend/config/prompts/no_login.yaml                   # NO_LOGIN_PROMPT
```

### 改造 3
```
backend/app/services/synthesizer.py     # 928 → 64 行（薄壳 re-export）
backend/app/api/chat.py                 # import 路径切换到 chat.orchestrator
backend/tests/test_synthesizer_refund.py # patches 切到 chat.* 命名空间
```

### 测试 mock 同步（3）
```
backend/tests/test_anti_hallucination.py    # patch chat.orchestrator.ProductTool + chat.stream_dispatcher.ProductTool
backend/tests/test_source_attribution.py    # 同上 + import 从 synthesizer → chat.prompt_assembler
backend/tests/test_synthesizer_refund.py    # patches → chat.refund_handler.* + chat.orchestrator.* + chat.stream_dispatcher.*
```

### 新增测试 2（commit 5 · 本次）
```
backend/tests/test_chat_prompt_assembler.py    # 11 用例（纯字符串处理函数）
backend/tests/test_chat_meta_contexts.py       # 7 用例（meta contexts 结构契约）
```

### 文档（commit 5）
```
docs/development/roadmap.md        # G6 行修订 + §3.4 S3 标 ✅
docs/development/current_status.md # v3（本文件）
docs/learning_log.md               # 追加 §27
```

---

## 3.1 Sprint 1-2 文件清单（历史归档 · 不再维护）

> Sprint 1 / Sprint 2 的文件清单已分别写入对应 commit message 与 docs/learning_log.md §25-26；本文件 v3 起只保留 Sprint 3 当下文件清单，避免冗长历史拖慢恢复效率。
> 需要查询 Sprint 1 / Sprint 2 文件范围时：`git log --stat <commit-sha>`。

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

### 4.3 单文件规模（Sprint 3 拆分后）
| 文件 | 行数 | 备注 |
|------|------|------|
| `chat/orchestrator.py` | **402** | ADR 预算 < 350，超 52 行（已知偏离，已记录到 roadmap S3） |
| `chat/prompt_assembler.py` | **276** | ADR 预算 < 250，超 26 行（已知偏离；7 个纯函数聚集） |
| `chat/refund_handler.py` | **222** | < 250，符合 |
| `chat/stream_dispatcher.py` | **78** | < 100，符合 |
| `chat/citation_formatter.py` | **14** | 占位文件，注释说明未来扩展位 |
| `synthesizer.py`（薄壳） | **64** | re-export + 文档说明删除计划 |

**已知偏离原因**：Sprint 3 把 928 行单体拆 5 模块时，发现 orchestrator 主类承担太多意图分发逻辑（M9.5+ 修复、多意图路由、退款 V2/V3 选择等），二次拆分需要更多业务理解；S4 业务规则 YAML 化后会更易拆。

### 4.4 删除计划（重要）
- `core/qwen.py` / `core/embedding.py` / `services/rerank.py` 删除计划 = **S4 末**
- `app/services/synthesizer.py`（薄壳）删除计划 = **S4 末**
- S3 / S4 期间必须保留（防止回归 + 兜住历史 import 路径）

---

## 5. 测试与验证结果

| 项 | 结果 |
|----|------|
| 全量 pytest（Sprint 3 末） | **168 passed**（含 18 个新纯函数测试：11 prompt_assembler + 7 meta_contexts） |
| Sprint 2 末 | 150 passed |
| Sprint 1 末 | 129 passed |
| 调用点 grep | `grep "from app.core.qwen\|from app.core.embedding" backend/app/services/` → **0 命中** |
| 反向依赖 grep | `grep "from app.services" backend/app/core/providers/` → **0 命中** |
| 旧 import 路径兜住 | `from app.services.synthesizer import Synthesizer` 仍可用（薄壳 re-export） |
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
| S3 | Synthesizer 拆分（928 → 5 模块 + 2 Prompt） | 🔴 P0 | G5（主要）+ G7 降级 | ✅ 完成 |
| S4 | 业务规则配置化（阈值 YAML 化 + 余 3 Prompt） | 🟠 P1 | G8 + G5 残留 | ⏸ 待办 |
| S5 | 目录对齐 CLAUDE.md §7.1（仅文档） | 🟢 P2 | G11-G13 | ⏸ 待办 |
| S6 | 多租户 MVP 预备 | 🟢 P2 | G9 | ⏸ 待办 |

Roadmap V1 已归档：`docs/development/archive/2026-07-11_roadmap_v1_archived.md`

---

## 7. 下一次恢复开发的入口

### 7.1 启动 Sprint 4 之前必读
1. `docs/development/roadmap.md` §S4（业务规则配置化范围）
2. `docs/decisions/2026-07-12-sprint-3-synthesizer-split.md`（S3 ADR + S4 启动前置）
3. `docs/governance/ai_development_rules.md`（AI 开发规则）
4. 本文件 §4（确认 Provider 抽象 + chat/ 子包约束未被破坏）

### 7.2 Sprint 4 范围预告
- 业务规则配置化（阈值 / 转人工 / 情绪 → YAML）
- **剩余 3 个业务 Prompt 抽取**（Sprint 3 仅抽 2/5，余下跨模块的 3 个由 S4 完成）：intent / query_rewriter / rerank
- 删除 `core/qwen.py` / `core/embedding.py` / `services/rerank.py` / `synthesizer.py` 薄壳（S4 末）
- orchestrator.py 402 行 + prompt_assembler.py 276 行二次拆分（依赖 S4 YAML 化后才能继续拆）

### 7.3 工作树清理
- 工作树当前 M 文件：`scripts/eval_hitk.py`（非 Sprint 修改，旧任务遗留，**不要混入 Sprint 4 commit**）
- 工作树当前 ?? 文件：`docs/README.md` / `docs/architecture/business.md` / `docs/governance/` / `chat_*.json` / `*.py` 临时文件 → 启动前评估是否清理

---

## 8. 未完成任务

### 8.1 Sprint 1-3 范围内：全部完成 ✅
### 8.2 项目整体（不属于 Sprint 1-3）
| 项 | 性质 | 说明 |
|----|------|------|
| `scripts/eval_hitk.py` 修改未 commit | 旧任务遗留 | 应单独评估归属（可能是 eval hit@K 脚本） |
| `docs/README.md` untracked | Phase 0 收尾 | 内容已写完，可单独 commit "docs: 子目录化首次提交" |
| `docs/architecture/business.md` untracked | Phase 0 收尾 | V3.1 业务架构基线 |
| `docs/governance/ai_development_rules.md` untracked | Phase 0 收尾 | AI 开发规则 |
| `chat_*.json` / `add_terms.py` / `insert_sections.py` / `merge_extra.py` / `restore_prefix.py` / `extra_sections.md` | 临时调试残留 | 启动前判断是否清理或归档 |

### 8.3 Sprint 4-S6
详见 §6 表格。

---

## 9. 当前禁止提前执行的事项

| # | 禁止 | 原因 |
|---|------|------|
| 1 | 删除 `core/qwen.py` / `core/embedding.py` / `services/rerank.py` / `app/services/synthesizer.py` | 删除计划 S4 末；S3/S4 期间作为兼容垫片保留 |
| 2 | 引入 `VectorStore` Protocol | YAGNI（V3+） |
| 3 | 引入第二个 Provider 实现（GPT / Claude / BGE） | YAGNI；当前 1 个实现 |
| 4 | 往 Provider 加 health_check / switch_model / fallback / metrics / cost / retry / breaker 等扩展方法 | YAGNI；真实需要时再加 |
| 5 | Provider 直接 `new` 具体类（绕过 `get_xxx_provider()`） | 违反依赖倒置；所有调用必须走工厂 |
| 6 | `core/providers/*.py` 反向依赖 `services/` | 破坏 §9.2.3 单向依赖 |
| 7 | `chat/` 子包之外的代码直接调用 chat 子包内部模块（违反就近原则） | §7.3 接口就近；调用方只能 import `chat.orchestrator.Synthesizer` |
| 8 | 启动 Sprint 4+ | 必须先 Sprint 3 完成且归档 |
| 9 | 把 Sprint 1-3 范围外的 untracked 文件混入 Sprint 4 commit | 违反 §5 Scope Lock |
| 10 | 把 Provider 改造反向迁移回 `from app.core.qwen import` 旧风格 | Sprint 1 切换成果回滚 |
| 11 | 把 synthesizer.py 厚壳化（恢复业务逻辑） | Sprint 3 切换成果回滚 |

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
读 CLAUDE.md V2.1 → 读 roadmap.md S4 → 读本文件 §4/§9 确认约束
→ 启动 Sprint 4 Step 1 任务分析（业务规则 YAML 化 + 余 3 Prompt 抽取）
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

**文件版本**：v3 · 2026-07-12 Sprint 3 完成时更新
**下次更新**：Sprint 4 启动前（轻量修订） / Sprint 4 完成时（重写 Sprint 段落）