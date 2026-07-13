# 项目当前状态 · 恢复记忆（2026-07-13 Sprint 4 阶段 1~4 完成后）

> 本文件是**会话级恢复记忆**，不是长期文档基线。
> 长期基线：`CLAUDE.md`（V2.1） / `docs/architecture/business.md` / `docs/development/roadmap.md`。
> 本文件下次启动开发时**先读**，然后决定是否仍相关（轻量修订即可）。
>
> **本次会话状态**：完成 Sprint 4 阶段 4（intent 业务规则 YAML 化，81 pattern + 2 实体正则），CI run #9 success；等待用户决定下一步（Phase 4 = query_rewriter.py 业务增强含其 YAML 化 / 或其他）。

---

## 0. 一句话状态

**Phase 0 治理 + Sprint 1/2/3 + Sprint 4 阶段 1~4 全部完成 ✅。**

**17 个 commit 已提交**（Sprint 1：4 / Sprint 2：4 / Sprint 3：5 / Sprint 4：4），**pytest 212 passed**，架构验收 🟢 7 / 🟡 4 / 🔴 0。

Phase 4（query_rewriter.py 业务增强 + 其 YAML 化）+ 删 legacy 薄壳 + 余 3 Prompt 抽取待启动。

---

## 1. 已完成的 Sprint（5/6 阶段 = 83%）

| Sprint | 主题 | commit 数 | 行数变化 | 关闭 Roadmap 缺口 | 状态 |
|--------|------|----------|---------|------------------|------|
| S1 | AI 三件套 Provider 抽象 | 4 | 新增 ~500 | G1 / G2 / G10 | ✅ 完成 |
| S2 | Prompt 基础设施（loader + 目录） | 4 | 新增 ~280 | G6（主要） | ✅ 完成 |
| S3 | Synthesizer 拆分 928→5 模块 + 2/5 Prompt | 5 | 928 → 1056（拆 5）+ 64（薄壳） | G5（主要）+ G7 缓解 | ✅ 完成 |
| S4 阶段 1-3 | 业务规则 YAML 化（config_loader + guard + refund 跨 3 文件） | 3 | 新增 ~430（loader + 2 YAML + 9 测试）+ 改 4 文件 | G8（部分） | ✅ 完成 |
| S4 阶段 4 | intent 业务规则 YAML 化（81 pattern + 2 实体正则） | 1 | 新增 ~358（intent.yaml + 14 测试）+ 改 intent_service | G8（intent） | ✅ 完成 |
| Phase 4 | query_rewriter.py 业务增强 + 其 YAML 化 | - | - | G8（剩余） | ⏸ 待办 |
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

### Sprint 4：业务规则 YAML 化（4 · 阶段 1+2+3+4）
```
38932ab  feat(services)    Sprint 4 阶段 1 - 业务规则配置加载器（config_loader）
5132176  feat(services)    Sprint 4 阶段 2 - guard 业务规则 YAML 化
70e5a3e  feat(services)    Sprint 4 阶段 3 - refund 业务规则 YAML 化（跨 3 文件）
efa729b  test(services)    test_guard_config 加 autouse fixture 隔离 config_loader 单例
1338a09  feat(services)    Sprint 4 阶段 4 - intent 业务规则 YAML 化（81 pattern + 2 正则）
```

> 注：Sprint 4 阶段 3 实际产生 2 commit（feature + test 修复），按 §3.4 最小修改拆分以便独立回滚。

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

## 3a. Sprint 4 关键变化（阶段 1+2+3）

### 3a.1 新建文件（5）

```
backend/app/services/config_loader.py             # 阶段 1 · Protocol + 工厂 + 异常体系（~190 行）
backend/config/business_rules/refund.yaml         # 阶段 3 · 2 字段（REFUND_WINDOW_DAYS / DELIVERY_OFFSET_DAYS）
backend/tests/test_refund_config.py               # 阶段 3 · 9 用例（3 文件 YAML 同步 / 公共 API / fail-fast）
# 阶段 1 + 2 已在之前 commit 建好：config_loader.py + guard.yaml + test_config_loader.py + test_guard_config.py
```

### 3a.2 改造文件（4 · 跨 3 模块共享 1 YAML）
```
backend/app/services/refund_graph.py          # 阶段 3 · 顶部 2 常量 → YAML 引用（+11 行）
backend/app/tools/refund_tool.py              # 阶段 3 · RefundTool.REFUND_WINDOW_DAYS → YAML（+7 行）
backend/app/services/order_lifecycle.py       # 阶段 3 · DELIVERY_OFFSET_DAYS → YAML（+8 行）
backend/tests/test_guard_config.py            # 阶段 3 后续 · autouse fixture + is→== 断言修复（+31 行）
```

### 3a.3 文档文件（2）
```
docs/development/roadmap.md                       # §3.5.1 S4 实绩记录（新增）
docs/learning_log.md                              # 追加 §28（按 CLAUDE.md §8 六段）
```

### 3a.4 Sprint 4 阶段 3 关键决策（无独立 ADR，按 CLAUDE.md §5.2 跨模块四要素口头审批）

| # | 要素 | 决议 |
|---|------|------|
| 1 | 业务原因 | `REFUND_WINDOW_DAYS = 7` 在 3 个文件硬编码（refund_graph / refund_tool / order_lifecycle），单文件迁移保留双真相源 bug 风险 |
| 2 | 接口变化 | 无新增接口；`RefundTool.REFUND_WINDOW_DAYS` 保留类属性语法（赋值为 YAML 值） |
| 3 | 影响范围 | 3 个生产文件 + 1 个新 YAML + 1 个新测试文件 |
| 4 | 隔离策略 | 3 文件同 commit；YAML 值与原硬编码完全一致（行为不变） |

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
       │                       │                  │
       ▼                       ▼                  ▼
services.prompt_loader  services.config_loader  core.providers.llm
  (Sprint 2 基础设施)    (Sprint 4 阶段 1)    (Sprint 1 Provider 抽象)
   config/prompts/      config/business_rules/        │
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

### 4.2a 业务规则配置层（Sprint 4 阶段 1+2+3）

| 文件 | 来源 | 用途 |
|------|------|------|
| `app/services/config_loader.py` | 阶段 1 新增 | Protocol + 工厂 + 异常体系；业务模块通过 `get_config_loader().load(name)` 读取 |
| `config/business_rules/guard.yaml` | 阶段 2 | 7 阈值 + 6 闲聊话术 |
| `config/business_rules/refund.yaml` | 阶段 3 | 2 字段（REFUND_WINDOW_DAYS / DELIVERY_OFFSET_DAYS） |

**消费方**：
- `services/guard.py`（阶段 2）→ 阈值 + 闲聊话术
- `services/refund_graph.py` + `tools/refund_tool.py` + `services/order_lifecycle.py`（阶段 3）→ 3 文件共享 1 YAML

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

### 4.5 架构验收结论（2026-07-13 Sprint 4 阶段 3 后更新）

**🟢 7 / 🟡 4 / 🔴 0**

| 维度 | 状态 | 说明 |
|------|------|------|
| §2 禁止行为 8 项 | 🟢 全过 | 无违反 |
| §9.1 Interface First | 🟢 Provider/LLM/Prompt + ConfigLoader 抽象 | 业务 Service 1 个实现未抽 Protocol（YAGNI 正确） |
| §9.1 Module Isolation | 🟢 无循环依赖 | chat/ + config_loader 内部互引方向单向 |
| §9.1 Dependency Inversion | 🟢 Sprint 1 成果 0 破坏 + Sprint 4 config_loader 通过工厂注入 | `grep` 验证 0 命中 `from app.core.qwen/embedding` |
| §9.4.2 配置与逻辑分离 | 🟢 Sprint 4 阶段 1+2+3 完成 | guard / refund 阈值已迁出代码 |
| §9.5 安全可观测 | 🟡 部分 | metrics import 已预留但未实际调用 |
| §9.6 Prompt 独立管理 | 🟢 2/5 YAML + Sprint 2 mtime | 余 3 个 S4 抽取 |
| 单文件预算 | 🟡 3 处偏离 | ADR §6 已声明，S4 二次拆消化 |

---

## 5. 测试结果

| 项 | 结果 |
|----|------|
| 全量 pytest（Sprint 4 阶段 3 末） | **198 passed**（含 Sprint 4 阶段 1+2+3 新增测试） |
| Sprint 3 末 | 168 passed |
| Sprint 2 末 | 150 passed |
| Sprint 1 末 | 129 passed |
| Sprint 4 阶段 3 新增测试分布 | test_refund_config 9 + test_config_loader 21（阶段 1）+ test_guard_config 6（阶段 2） |
| 测试架构分层 | 契约测试（纯函数） + 集成测试（Mock SSE 协议） + 业务回归 + 配置加载（fail-fast） |
| patch namespace 完整性 | 100% 命中 `chat.*`，0 残留旧 `synthesizer.*` 命名空间 |
| CI | GitHub Actions run #7 `success`（commit `efa729b`） |

**测试启动方式**：
```bash
cd E:/智能客服/backend
JWT_SECRET="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4" \
DATABASE_URL="mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4" \
  python -m pytest tests/ -q
```

**预期输出**：`198 passed in ~3.3s`

---

## 6. 下次启动入口（用户决定）

### 6.1 选项 A：Sprint 4 阶段 4 — 业务规则 YAML 化收尾（推荐）

| 任务 | 关闭缺口 | 预计改动量 |
|------|----------|-----------|
| `intent.yaml`（intent_service 阈值） | G8（剩余） | 小（阶段 2/3 模式复用） |
| `query_rewriter.yaml`（query_rewriter 阈值） | G8（剩余） | 小（同上） |
| 测试（test_intent_config + test_query_rewriter_config） | - | 小（直接复用 test_refund_config 模式） |

**价值**：关闭 G8 缺口（roadmap §3.5 全部落地），让所有业务规则统一走 YAML。

### 6.2 选项 B：Phase 4（query_rewriter.py 业务能力增强）

不在本文件详细分析范围；用户提到"等阶段总结后再启动 Phase 4（query_rewriter.py）"。

### 6.3 选项 C：Sprint 5 — 目录对齐 CLAUDE.md §7.1（仅文档）

最简单的工作量，纯文档同步。详见 roadmap §3.6。

### 6.4 Sprint 4 阶段 4 启动必读（4 文件）

```
1. CLAUDE.md                                            # V2.1 治理基线
2. docs/development/roadmap.md                           # §3.5 + §3.5.1 S4 实绩
3. docs/learning_log.md §28                              # Sprint 4 阶段 1+2+3 完整复盘（含 3 文件跨模块迁移 + test 污染修复）
4. docs/development/current_status.md                   # 本文件 §4-§6
```

### 6.5 AI 6 步法 · Step 1 任务分析起点（任一选项均需走完）

**输入**（用户待决）：
- 选项 A：intent.yaml + query_rewriter.yaml 迁移（同模式复用阶段 3 经验）
- 选项 B：query_rewriter.py 业务能力增强（需先看业务架构 V3.1）
- 选项 C：纯文档同步（CLAUDE.md §7.1 对齐实际目录）

**当前代码现状**（选项 A 扫描起点）：
| 模块 | 待改点 |
|------|--------|
| `services/intent_service.py` | 阈值硬编码（待 grep 确认） |
| `services/query_rewriter.py` | 阈值硬编码（待 grep 确认） |

**已知风险（选项 A）**：
1. 业务规则 YAML 化模式已被阶段 2/3 验证，复用风险低
2. intent 与 query_rewriter 都跨模块边界（intent 服务于 synthesizer，query_rewriter 服务于 RAG），按 CLAUDE.md §5.2 应列四要素
3. 删 legacy 薄壳前 grep 全仓确认 0 引用（仍属选项 B 范围）

---

## 7. 下次恢复开发必须先读的文档（7 项）

> 按重要性 + 上下文需求排序。开发前全读完。

| 序 | 文档 | 路径 | 读的目的 |
|----|------|------|----------|
| 1 | 本文件 | `docs/development/current_status.md` | 当前状态 + Sprint 4 阶段 4 / Phase 4 入口 |
| 2 | CLAUDE.md | `E:\智能客服\CLAUDE.md` | V2.1 治理基线（AI 6 步法 / 13 反例 / 9 架构规则） |
| 3 | Roadmap V2 | `docs/development/roadmap.md` | S1-S6 全景 + G 缺口表 + §S4 实绩（§3.5.1） |
| 4 | Sprint 3 ADR | `docs/decisions/2026-07-12-sprint-3-synthesizer-split.md` | Sprint 3 架构基线（拆分粒度 / Prompt 范围 / 兜底策略） |
| 5 | AI 6 步法速查 | `docs/governance/ai_development_rules.md` | 反例清单 13 条 + 跨模块 4 要素 + Stop-Loss 8 问 |
| 6 | 学习日志 | `docs/learning_log.md` §27-28 | Sprint 3 / Sprint 4 阶段 1+2+3 复盘（3 文件跨模块迁移 + test 污染修复） |
| 7 | 业务架构 V3.1 | `docs/architecture/business.md` | 业务边界 + 数据责任（修改业务时必读） |

---

## 8. 当前禁止提前执行的事项（截止 Sprint 4 阶段 4 / Phase 4 启动前）

| # | 禁止 | 原因 |
|---|------|------|
| 1 | 删除 `core/qwen.py` / `core/embedding.py` / `services/rerank.py` / `app/services/synthesizer.py` | 删除计划 S4 末；S3/S4 期间作为兼容垫片保留 |
| 2 | 启动 Sprint 4 阶段 4 或 Phase 4 | 必须先读完本文 §7 的 7 个文档 |
| 3 | 把 Sprint 1-4 阶段 3 范围外的 untracked 文件混入下次 commit | 违反 §5 Scope Lock |
| 4 | 把 Provider 改造反向迁移回 `from app.core.qwen import` | Sprint 1 切换成果回滚 |
| 5 | 把 synthesizer.py 厚壳化（恢复业务逻辑） | Sprint 3 切换成果回滚 |
| 6 | `chat/` 子包之外的代码直接调用 chat 子包内部模块 | §7.3 接口就近；调用方只能 import `chat.orchestrator.Synthesizer` |
| 7 | 引入第二个 Provider 实现 / 加 health_check 等扩展方法 | YAGNI；真实需要时再加 |
| 8 | `core/providers/*.py` 反向依赖 `services/` | 破坏 §9.2.3 单向依赖 |
| 9 | 直接 `new YAMLConfigLoader(base_dir)` 绕过工厂 | 违反 §9.1 Interface First；业务只能 `get_config_loader().load(name)` |

---

## 9. 工作树状态（2026-07-13 Sprint 4 阶段 3 完成后）

### 9.1 Sprint 1-4 阶段 3 范围内：全部归档 + push 完成 ✅
- 16 个 commit 全部 push 到 Gitee origin + GitHub github 双 remote（CI run #7 success）
- 工作树干净（除本次会话文档更新）

### 9.2 本次会话文档更新（待 commit 归档）
- `docs/development/roadmap.md` · §3.5.1 新增 S4 实绩记录
- `docs/learning_log.md` · 追加 §28 Sprint 4 阶段 1+2+3 六段复盘
- `docs/development/current_status.md` · v3 → v4 整体重写（§0-§11 同步 Sprint 4 状态）

### 9.3 项目整体残留
无新增残留（2026-07-13 Sprint 3 末 v3 已清理磁盘临时文件 + 核查 `.gitignore`）。

---

## 10. 下一步可选动作（用户决定）

> 当前会话暂停，等待用户指示。
> 已完成 Sprint 4 阶段 1+2+3（按 CLAUDE.md §8 八件套交付文档）。下一步选项：

| 选项 | 动作 | 适用场景 |
|------|------|----------|
| A | 启动 **Sprint 4 阶段 4**：intent.yaml + query_rewriter.yaml 迁移（同模式复用阶段 3 经验） | 关闭 G8 缺口收尾 |
| B | 启动 **Phase 4**：query_rewriter.py 业务能力增强 | 业务架构 V3.1 演进 |
| C | 启动 **Sprint 5**：CLAUDE.md §7.1 目录对齐（仅文档） | 最简单工作量 |
| D | 先 git push 本次会话文档更新（roadmap / learning_log / current_status） | 远端备份优先 |

---

## 11. 一句话恢复提示

```
下次启动：
1. 读 CLAUDE.md V2.1 + 本文件 + roadmap.md §3.5.1 + learning_log.md §28（按 §7 顺序）
2. 决定 §10 可选动作 A/B/C/D
3. 进入 Sprint 4 阶段 4 时按 §6.5 Step 1 任务分析模板先输出方案 → 等用户确认 → 再开发
```

---

**文件版本**：v4 · 2026-07-13 Sprint 4 阶段 1+2+3 完整闭环 + 架构验收后更新
**下次更新**：Sprint 4 阶段 4 启动前（轻量修订） / Sprint 4 完成时（重写 Sprint 段落）

**本次会话状态**：✅ Sprint 4 阶段 1+2+3 完成 + 文档归档；等待用户决定 §10 选项 A/B/C/D
