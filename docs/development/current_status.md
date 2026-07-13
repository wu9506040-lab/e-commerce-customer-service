# 项目当前状态 · 恢复记忆（2026-07-12 Sprint 3 完整闭环后）

> 本文件是**会话级恢复记忆**，不是长期文档基线。
> 长期基线：`CLAUDE.md`（V2.1） / `docs/architecture/business.md` / `docs/development/roadmap.md`。
> 本文件下次启动开发时**先读**，然后决定是否仍相关（轻量修订即可）。
>
> **本次会话状态**：完整执行完 Sprint 3 全部 5 commit + 架构验收通过；暂停开发，等待下次启动 Sprint 4。

---

## 0. 一句话状态

**Phase 0 治理 + Sprint 1 AI Provider 抽象 + Sprint 2 Prompt 基础设施 + Sprint 3 Synthesizer 拆分 全部完成 ✅。**

**13 个 commit 已提交**（Sprint 1：4 / Sprint 2：4 / Sprint 3：5），**pytest 168 passed**，架构验收 🟢 6 / 🟡 5 / 🔴 0。

进入 **Sprint 4 待启动**（业务规则 YAML 化 + 余 3 Prompt 抽取 + 删 legacy 薄壳 + 二次拆分）。

---

## 1. 已完成的 Sprint（4/6 = 67%）

| Sprint | 主题 | commit 数 | 行数变化 | 关闭 Roadmap 缺口 | 状态 |
|--------|------|----------|---------|------------------|------|
| S1 | AI 三件套 Provider 抽象 | 4 | 新增 ~500 | G1 / G2 / G10 | ✅ 完成 |
| S2 | Prompt 基础设施（loader + 目录） | 4 | 新增 ~280 | G6（主要） | ✅ 完成 |
| S3 | Synthesizer 拆分 928→5 模块 + 2/5 Prompt | 5 | 928 → 1056（拆 5）+ 64（薄壳） | G5（主要）+ G7 缓解 | ✅ 完成 |
| S4 | 业务规则 YAML 化 + 余 3 Prompt | - | 待启动 | G8 + G5 残留 | ⏸ 待办 |
| S5 | 目录对齐 CLAUDE.md §7.1（仅文档） | - | - | G11-G13 | ⏸ 待办 |
| S6 | 多租户 MVP 预备 | - | - | G9 | ⏸ 待办 |

---

## 2. 所有 commit（13 个 · 按 Sprint 分组）

### Sprint 1：AI Provider 抽象（4）
```
24fed9a  feat(core)        新增 LLM/Embedding/Rerank Provider 抽象层
674ac50  refactor(services) 业务模块切到 Provider 抽象（13 调用点）
54a4b52  test(core)        新增 Provider 契约测试 + mock 路径同步
afd6b50  chore(core)       legacy qwen/embedding 标 deprecated + 文档同步
```

### Sprint 2：Prompt 基础设施（4）
```
1f705fc  chore(deps)       新增 PyYAML==6.0.2 锁版本
910663c  feat(services)    新增 prompt_loader 统一加载器（Protocol + Factory）
05d5965  chore(config)     PROMPT_DIR + config/prompts 架子 + Dockerfile COPY
68d5700  test(services)    prompt_loader 单元测试 21 用例 + mtime 顺序 bugfix
```

### Sprint 3：Synthesizer 拆分 928→5 模块（5）
```
63e7044  docs(sprint3)     Sprint 3 启动 ADR（Synthesizer 拆分 + 范围 A 决议）
7f343fd  feat(prompts)     新建 agent + no_login YAML（Sprint 3 抽 2/5 Prompt）
a8bea00  feat(chat)        cp 完整 4 个新模块（安全网，不动旧代码）
5ee01f6  refactor(synthesizer) 切换 import + 旧 synthesizer 薄壳化 + refund_v2/v3 移出
d0791fe  test+docs(chat)   新增 18 个纯函数单测 + 文档收尾（本 commit 闭环）
```

---

## 3. Sprint 3 关键变化

### 3.1 新建文件（10）

```
backend/app/services/chat/__init__.py                 # 空
backend/app/services/chat/orchestrator.py            # 402 行 Synthesizer 主类
backend/app/services/chat/prompt_assembler.py        # 276 行 7 个纯字符串函数
backend/app/services/chat/stream_dispatcher.py       # 78 行 stream_llm + 滑窗
backend/app/services/chat/refund_handler.py          # 222 行 handle_refund_v2/v3
backend/app/services/chat/citation_formatter.py      # 14 行占位
backend/config/prompts/agent.yaml                    # SYSTEM_PROMPT_BASE
backend/config/prompts/no_login.yaml                 # NO_LOGIN_PROMPT
backend/tests/test_chat_prompt_assembler.py          # 11 用例
backend/tests/test_chat_meta_contexts.py             # 7 用例
```

### 3.2 改造文件（4）
```
backend/app/services/synthesizer.py        # 928 → 64 行（薄壳 re-export）
backend/app/api/chat.py                    # import 切到 chat.orchestrator
backend/tests/test_anti_hallucination.py   # patches → chat.* namespace
backend/tests/test_source_attribution.py   # 同上
backend/tests/test_synthesizer_refund.py   # 同上 + 改调 handle_refund_v3 模块级函数
```

### 3.3 文档文件（4）
```
docs/decisions/2026-07-12-sprint-3-synthesizer-split.md   # Sprint 3 ADR（新）
docs/development/roadmap.md                                # G6 行 + §3.4 S3 ✅
docs/development/current_status.md                         # v3（本文件）
docs/learning_log.md                                       # 追加 §27（按 CLAUDE.md §8 六段）
```

---

## 4. 当前代码架构

### 4.1 依赖方向（运行时 · 单向 ✅）

```
                api/chat.py
                     │
                     ▼
       chat/orchestrator.Synthesizer      ← Sprint 3 新增编排入口
                     │
       ┌─────────────┼──────────────────┐
       ▼             ▼                  ▼
chat.prompt_assembler  chat.refund_handler  chat.stream_dispatcher
       │                                       │
       ▼                                       ▼
services.prompt_loader                  core.providers.llm
  (Sprint 2 基础设施)                  (Sprint 1 Provider 抽象)
                                               │
                                               ▼
                                       DashScope OpenAI 兼容
```

### 4.2 chat/ 子包内部 5 模块职责

| 模块 | 行数 | 职责 |
|------|------|------|
| `orchestrator.py` | 402 | Synthesizer.run_stream + 4 个 `_handle_<intent>` 意图分发 |
| `prompt_assembler.py` | 276 | 7 个纯字符串拼接函数 + SYSTEM/NO_LOGIN 走 prompt_loader |
| `stream_dispatcher.py` | 78 | `_LLM_SEMAPHORE` + `stream_llm` + `stream_simple` + `search_by_keyword_window` |
| `refund_handler.py` | 222 | `handle_refund_v2`（双轨制）+ `handle_refund_v3`（V3 LangGraph） |
| `citation_formatter.py` | 14 | 占位（未来引用标签规范化） |

### 4.3 已知 ADR 预算偏离（Sprint 3 ADR §6 已声明 · S4 收尾）

| 文件 | 实际 | ADR 预算 | 偏离 | 改进路径 |
|------|------|----------|------|----------|
| orchestrator.py | 402 | < 350 | +52 | S4 拆 `_handle_product` / `_try_direct_answer_order` |
| prompt_assembler.py | 276 | < 250 | +26 | S4 拆 `_build_context_block`（53 行独立） |
| chat/ 文件数 | 6 | ≤ 4 | +2 | 行数 vs 文件数取舍选了行数让步 |

### 4.4 删除计划（S4 末）
- `core/qwen.py` / `core/embedding.py` / `services/rerank.py`（Sprint 1 遗留薄壳）
- `app/services/synthesizer.py`（Sprint 3 薄壳 · 兜住历史 import 路径）
- 删除前置 grep 验证：
  ```bash
  grep -rn "from app.core.qwen\|from app.core.embedding" backend/app/services/  # 应 0 命中
  grep -rn "from app.services.synthesizer\|from app.services.rerank" backend/  # 应只剩 synthesizer.py 自身
  ```

### 4.5 架构验收结论（2026-07-12 完成）

**🟢 6 / 🟡 5 / 🔴 0**

| 维度 | 状态 | 说明 |
|------|------|------|
| §2 禁止行为 8 项 | 🟢 全过 | 无违反 |
| §9.1 Interface First | 🟢 Provider/LLM/Prompt 抽象 | 业务 Service 1 个实现未抽 Protocol（YAGNI 正确） |
| §9.1 Module Isolation | 🟢 无循环依赖 | chat/ 内部互引方向单向 |
| §9.1 Dependency Inversion | 🟢 Sprint 1 成果 0 破坏 | `grep` 验证 0 命中 `from app.core.qwen/embedding` |
| §9.5 安全可观测 | 🟡 部分 | metrics import 已预留但未实际调用 |
| §9.6 Prompt 独立管理 | 🟢 2/5 YAML + Sprint 2 mtime | 余 3 个 S4 抽取 |
| 单文件预算 | 🟡 3 处偏离 | ADR §6 已声明，S4 二次拆消化 |

---

## 5. 测试结果

| 项 | 结果 |
|----|------|
| 全量 pytest（Sprint 3 末） | **168 passed**（含 Sprint 3 新增 18 个纯函数测试） |
| Sprint 2 末 | 150 passed |
| Sprint 1 末 | 129 passed |
| Sprint 3 新增测试分布 | test_chat_prompt_assembler 11 + test_chat_meta_contexts 7 |
| 测试架构分层 | 契约测试（纯函数） + 集成测试（Mock SSE 协议） + 业务回归 |
| patch namespace 完整性 | 100% 命中 `chat.*`，0 残留旧 `synthesizer.*` 命名空间 |

**测试启动方式**：
```bash
cd E:/智能客服/backend
JWT_SECRET="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" \
DATABASE_URL="mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4" \
  python -m pytest tests/ -q
```

**预期输出**：`168 passed in ~17s`

---

## 6. Sprint 4 启动入口

### 6.1 Sprint 4 范围预告（按 ADR + roadmap）

| 任务 | 关闭缺口 | 预计改动量 |
|------|----------|-----------|
| 业务规则配置化（阈值 / 转人工 / 情绪 → YAML） | **G8** | 中（涉及 4-5 个 service） |
| **余 3 个跨模块 Prompt 抽取**（intent / query_rewriter / guard_chitchat） | **G5 残留** | 中（拆 3 个独立 commit） |
| 删除 `core/qwen.py` / `core/embedding.py` / `services/rerank.py` / `services/synthesizer.py` 薄壳 | - | 小（grep + delete） |
| orchestrator.py + prompt_assembler.py 二次拆分（消化 ADR 预算偏离） | §4.3 表 | 小（2 个独立 commit） |
| 顺手在 chat/ 内部补 2-3 个 metrics 埋点 | §9.5.2 | 极小 |

### 6.2 Sprint 4 启动必读（4 文件）

```
1. CLAUDE.md                                            # V2.1 治理基线
2. docs/development/roadmap.md                           # §S4 范围 + G 缺口表
3. docs/decisions/2026-07-12-sprint-3-synthesizer-split.md  # Sprint 3 架构基线（4 决定 + 5 commit 经验）
4. docs/development/current_status.md                   # 本文件 §4-§6
```

### 6.3 Sprint 4 AI 6 步法 · Step 1 任务分析起点

**输入**：
- 需求：业务规则配置化 + 余 3 Prompt 抽取 + 删 legacy 薄壳
- 优先级：P1（业务规则 P1 + Prompt 抽取 P2）
- 涉及模块预估：guard.py / intent_service.py / query_rewriter.py / services/rerank.py（业务规则）/ 4 个 service（Prompt 抽取）

**当前代码现状**（扫描起点）：
| 模块 | 待改点 |
|------|--------|
| `services/guard.py:55-67` | 硬编码阈值（转人工 / 情绪 / max_tokens） |
| `services/intent_service.py` | Prompt 硬编码在代码中 |
| `services/query_rewriter.py` | Prompt 硬编码 |
| `services/rerank.py` | Prompt 硬编码 + 已变薄壳（Sprint 1 切换后业务迁入 core/providers/rerank） |
| `services/guard.py` | `CHITCHAT_RESPONSES` 硬编码列表 |

**已知风险**：
1. 业务规则 YAML 化需要先和用户确认规则优先级（默认值 / 覆盖层）
2. 余 3 Prompt 抽取是跨 ≥ 4 模块改动，按 CLAUDE.md §5.2 应列四要素
3. 删 legacy 薄壳前 grep 全仓确认 0 引用

---

## 7. 下次恢复开发必须先读的文档（7 项）

> 按重要性 + 上下文需求排序。开发前全读完。

| 序 | 文档 | 路径 | 读的目的 |
|----|------|------|----------|
| 1 | 本文件 | `docs/development/current_status.md` | 当前状态 + Sprint 4 入口 |
| 2 | CLAUDE.md | `E:\智能客服\CLAUDE.md` | V2.1 治理基线（AI 6 步法 / 13 反例 / 9 架构规则） |
| 3 | Roadmap V2 | `docs/development/roadmap.md` | S1-S6 全景 + G 缺口表 + §S4 范围 |
| 4 | Sprint 3 ADR | `docs/decisions/2026-07-12-sprint-3-synthesizer-split.md` | Sprint 3 架构基线（拆分粒度 / Prompt 范围 / 兜底策略） |
| 5 | AI 6 步法速查 | `docs/governance/ai_development_rules.md` | 反例清单 13 条 + 跨模块 4 要素 + Stop-Loss 8 问 |
| 6 | 学习日志 | `docs/learning_log.md` §26-27 | Sprint 2 / Sprint 3 复盘（踩过的坑） |
| 7 | 业务架构 V3.1 | `docs/architecture/business.md` | 业务边界 + 数据责任（修改业务时必读） |

---

## 8. 当前禁止提前执行的事项（截止 Sprint 4 启动前）

| # | 禁止 | 原因 |
|---|------|------|
| 1 | 删除 `core/qwen.py` / `core/embedding.py` / `services/rerank.py` / `app/services/synthesizer.py` | 删除计划 S4 末；S3/S4 期间作为兼容垫片保留 |
| 2 | 启动 Sprint 4 | 必须先读完本文 §7 的 7 个文档 |
| 3 | 把 Sprint 1-3 范围外的 untracked 文件混入 Sprint 4 commit | 违反 §5 Scope Lock |
| 4 | 把 Provider 改造反向迁移回 `from app.core.qwen import` | Sprint 1 切换成果回滚 |
| 5 | 把 synthesizer.py 厚壳化（恢复业务逻辑） | Sprint 3 切换成果回滚 |
| 6 | `chat/` 子包之外的代码直接调用 chat 子包内部模块 | §7.3 接口就近；调用方只能 import `chat.orchestrator.Synthesizer` |
| 7 | 引入第二个 Provider 实现 / 加 health_check 等扩展方法 | YAGNI；真实需要时再加 |
| 8 | `core/providers/*.py` 反向依赖 `services/` | 破坏 §9.2.3 单向依赖 |

---

## 9. 工作树状态（2026-07-12 暂停点）

### 9.1 Sprint 1-3 范围内：全部归档完成 ✅
- 13 个 commit 全部 push 前（如需 push 见下方"下一步可选动作"）
- 工作树除 Sprint 1-3 范围外的旧文件残留外，干净

### 9.2 项目整体残留（不属于 Sprint 1-3 · 不混入 Sprint 4）

**⚠️ 2026-07-13 现场核查：上版（v2）所列 6 项清单全部过期**，以本节为准：

| 项 | 上版说法 | 实际情况（2026-07-13 核查） |
|----|----------|-----------------------------|
| `scripts/eval_hitk.py` 修改未 commit | 待单独 commit | ❌ working tree clean，无修改 |
| `docs/README.md` untracked | 待单独 commit | ❌ 已存在 + tracked（commit `1f92dee`，7769 bytes） |
| `docs/architecture/business.md` untracked | 待单独 commit | ❌ 已存在 + tracked（26548 bytes，V3.1 业务架构基线） |
| `docs/governance/ai_development_rules.md` untracked | 待单独 commit | ❌ 已存在 + tracked（35731 bytes） |
| `chat_*.json` 等临时调试残留 | 建议归档到 `scripts/_debug_archive/` | ❌ 11 个 json 全被 `.gitignore` 排除（`/chat_*.json` 等）；py/md 文件压根不存在 |
| `cookies.txt`（上版未列） | — | ✅ 已被 `.gitignore` 排除（`/cookies.txt`） |

**当前实际残留（2026-07-13）**：仅 `docs/development/current_status.md` 自身修改（v2→v3 轻量修订），2026-07-13 同步 commit 归档。
磁盘临时文件（11 json + cookies.txt）已删除，`.gitignore` 已配置无需再动。

---

## 10. 下一步可选动作（用户决定）

> 当前会话暂停，等待用户指示。

| 选项 | 动作 | 适用场景 |
|------|------|----------|
| A | ~~按"9.2 残留文件清理"逐项单独 commit~~（**作废**：2026-07-13 已在本会话完成清理 + commit，见 §9.2 核查表） | 工程整洁优先 |
| B | 直接进入 Sprint 4（先读本文 §7 7 个文档） | 推进 Sprint 主线 |
| C | 先 git push 所有 13 commit（Gitee origin + GitHub github 双 remote） | 远端备份优先 |
| D | 仅整理工作树（清理 / 归档临时文件）但不 commit | 暂缓提交 |

---

## 11. 一句话恢复提示

```
下次启动：
1. 读 CLAUDE.md V2.1 + 本文件 + roadmap.md S4 + Sprint 3 ADR（按 §7 顺序）
2. 决定 §10 可选动作 A/B/C/D
3. 进入 Sprint 4 时按 §6.3 Step 1 任务分析模板先输出方案 → 等用户确认 → 再开发
```

---

**文件版本**：v3 · 2026-07-12 Sprint 3 完整闭环 + 架构验收后更新
**下次更新**：Sprint 4 启动前（轻量修订） / Sprint 4 完成时（重写 Sprint 段落）

**本次会话状态**：✅ 已暂停开发，等待下次启动指令
