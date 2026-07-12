# Sprint 3 启动决策 — Synthesizer 拆分（2026-07-12）

> **决策记录（Sprint 开工）**：roadmap §3.4 启动条件 + 范围细化（范围 A）。
> **状态**：⏸ 进行中（用户已确认范围）
> **取代**：无（roadmap V2 §3.4 是基线，本 ADR 是细化）
> **维护者**：Tech Lead
> **最近更新**：2026-07-12

---

## 1. 目标

**主目标**：拆 synthesizer.py（928 行万能模块）→ 4 个新模块（orchestrator / prompt_assembler / stream_dispatcher / citation_formatter），关闭 **G7**。

**附带目标**：**G5 部分关闭**——将 synthesizer.py 内 2 段硬编码 Prompt 抽到 S2 架子就位的 `config/prompts/*.yaml`（agent + no_login）。

## 2. 范围 vs 不范围

### 2.1 范围 A（已确认）

| ✅ 范围内 | 说明 |
|----------|------|
| 拆分 synthesizer.py 为 `services/chat/` 4 个新模块 | 关闭 **G7** |
| 抽 2 个业务 YAML：`config/prompts/agent.yaml` + `no_login.yaml` | 关闭 **G5** 部分（synthesizer 范围内）|
| 接入 S2 创建的 `prompt_loader.load()` | 利用 S2 基础设施 |
| synthesizer.py 改写为薄壳（re-export `Synthesizer`） | 向后兼容 api/chat.py 已有的 import |
| 单文件 ≤ 250 行 / 全模块 ≤ 4 文件 / 跨模块 import ≤ 5 模块 | roadmap §3.4 验证标准 |

### 2.2 不范围（明确禁止）

| ❌ 不做 | 理由 |
|--------|------|
| **同步抽 5 个 YAML（不只 2 个）** | 范围 A 决议；intent/rerank/query_rewriter/guard_chitchat 在其他 service，抽它们跨 ≥ 4 模块，**违反 §5 Scope Lock** |
| 改业务逻辑（不改任何 _handle_* 的工作流） | roadmap §3.4 不范围 #2 |
| 改 Prompt 内容（仅迁移位置不改字） | roadmap §3.4 不范围 #2 |
| 改 API（仍是 `/api/chat`）| roadmap §3.4 不范围 #3 |
| 引入新依赖 | roadmap §3.4 不范围 #4 |
| 删除 `core/qwen.py` / `core/embedding.py` / `services/rerank.py` | current_status §9 禁止提前删；保留 S4 末 |
| 引入 VectorStore Protocol / 第二 Provider | YAGNI；S1 已 review 通过 |
| 引入第二个 Prompt 实现 / DB 存储 / 租户级覆盖 | §3.3 YAGNI |

### 2.3 跨模块 Prompt 抽取安排

intent / rerank / query_rewriter / guard_chitchat 这 4 个 Prompt 不在 synthesizer 范围内，**留给后续 Sprint 各自拆**：

| YAML | 所在 service | 安排 |
|------|-------------|------|
| `intent.yaml` | intent_service.py | Sprint 3 后单独立 Sprint 拆 intent_service（不在 Sprint 3 范围）|
| `rerank.yaml` | rag/pipeline.py 或 services/rerank.py | Sprint 4（业务规则配置化）一并抽 |
| `query_rewriter.yaml` | query_rewriter.py | 跟随 Sprint 4 拆分 |
| `guard_chitchat.yaml` | guard.py | 跟随 Sprint 4 拆分 |

## 3. 文件清单

| 操作 | 路径 | 来源 |
|------|------|------|
| 新建 | `backend/app/services/chat/__init__.py` | — |
| 新建 | `backend/app/services/chat/orchestrator.py` | synthesizer.py 的 `class Synthesizer` 框架部分 |
| 新建 | `backend/app/services/chat/prompt_assembler.py` | synthesizer.py 的 7 个模块级 prompt/context 函数 |
| 新建 | `backend/app/services/chat/stream_dispatcher.py` | synthesizer.py 的 `_stream_llm` / `_stream_simple` / `_search_by_keyword_window` / `_LLM_SEMAPHORE` |
| 新建 | `backend/app/services/chat/citation_formatter.py` | **新增拆分点**：引用标签格式化（先建空壳）|
| 新建 | `backend/config/prompts/agent.yaml` | synthesizer.py:48-63 `SYSTEM_PROMPT_BASE` |
| 新建 | `backend/config/prompts/no_login.yaml` | synthesizer.py:66-69 `NO_LOGIN_PROMPT` |
| 修改 | `backend/app/services/synthesizer.py`（缩为 ~30 行 re-export） | 删除所有搬迁过的逻辑 |
| 新建 | `backend/tests/test_chat_prompt_assembler.py` | 新增测试（§9.7 模块可独立测） |
| 新建 | `backend/tests/test_chat_meta_contexts.py` | 新增测试（前端协议契约）|
| 修改 | `docs/development/current_status.md` | v3 更新（S3 ✅ + S4 待启动）|
| 修改 | `docs/development/roadmap.md` | S3 状态 ✅ + G5/G7 关闭标注 |
| 修改 | `docs/learning_log.md` | 追加 §27 |

## 4. 拆分边界（细化）

| 新模块 | 来源方法 | 行数预算 | 跨模块 import（仅限） |
|--------|----------|----------|---------------------|
| `orchestrator.py` | `run_stream` + `_try_direct_answer_order` + `_handle_order` + `_handle_refund_v2` + `_handle_refund_v3` + `_handle_product` + `_handle_policy` + `_DIRECT_ANSWER_PATTERNS` | **< 350 行** | metrics, intent_service, order_service, policy_service, refund_graph, refund_service, rag.pipeline (fallback), session_service.ANONYMOUS_USER_ID |
| `prompt_assembler.py` | `_build_context_block` + `_build_chat_prompt` + `_format_tool_result` + `_format_policy_docs` + `_format_history` + `_build_meta_contexts` + `_extract_order_no_from_history` + 2 个 prompt YAML 加载 | **< 250 行** | tools.product_tool, order_service, session_service.ANONYMOUS_USER_ID, prompt_loader, **不**调 core.providers.llm |
| `stream_dispatcher.py` | `_stream_llm` + `_stream_simple` + `_search_by_keyword_window` + `_LLM_SEMAPHORE` | **< 80 行** | core.providers.llm, tools.product_tool, metrics |
| `citation_formatter.py` | **空壳**（标注未来引用标签规范的位置；当前无逻辑） | **< 30 行** | — |

**单文件 < 250 行目标 + 全部 < 4 文件**：满足 roadmap §3.4 验证标准。

## 5. §4.2 跨模块例外（4 要素）

实际上**不算跨模块改动**——拆分不引入新跨模块依赖（orchestrator 仍依赖原 9 个 service，prompt_assembler 仍调用 ProductTool/OrderService）。但 import 调用面有变化，需要明确：

| # | 要素 | 内容 |
|---|------|------|
| 1 | 业务原因 | 拆 synthesizer 928 → 4 模块；G7 缺口要求"高内聚低耦合"；新模块化后，未来业务逻辑新增（如新增 intent 处理）只需扩 orchestrator 一个文件，不动 prompt_assembler / stream_dispatcher |
| 2 | 接口变化 | `from app.services.synthesizer import Synthesizer` 仍有效（薄壳 re-export）；prompt_assembler 新增 `build_chat_prompt()` 等公开函数 |
| 3 | 影响范围 | 仅 `services/synthesizer.py`（缩为薄壳）+ `services/chat/`（新）；**不影响** `api/chat.py` / 其他 service / ORM / 数据库 / 部署 |
| 4 | 隔离策略 | commit 3 先 cp 完整 4 模块（不改旧代码）+ 跑 150 PASS 验证双份代码等价；commit 4 才切 import；任意 commit 可独立回滚 |

## 6. 验证计划（§6 验证分级）

| 类型 | 内容 | 通过判据 |
|------|------|----------|
| 单元 | `tests/test_chat_prompt_assembler.py`（约 5-6 用例）| 全 PASS |
| 单元 | `tests/test_chat_meta_contexts.py`（约 3-4 用例）| 全 PASS |
| 回归 | `tests/test_synthesizer_refund.py`（不改测试）| 不改测试仍 PASS |
| 全量 | `pytest tests/` | 维持 150+ PASS 无回归 |
| 反向依赖 | `grep "from app.services.chat" backend/app/services/` | 0 命中（chat/ 是被消费的，不应反向被 services/ 引用）|
| 单文件规模 | `wc -l services/chat/*.py` | 最大 < 350 行，prompt_assembler < 250 |
| Prompt 字面量 | `grep "你是一个专业的\|SYSTEM_PROMPT_BASE\|NO_LOGIN_PROMPT" backend/app/services/` | 仅命中 prompt_assembler.py（加载逻辑）+ config/prompts YAML |
| 跨模块 import | `grep "from app.services.intent_service\|...\\|rag.pipeline" backend/app/services/chat/` | 命中 ≤ 5 模块（验证后实际记录数字）|

## 7. commit 节奏（5 commit）

```
commit 1 (本次)：docs(decisions) Sprint 3 启动 ADR（本文件）
commit 2:        feat(prompts) agent.yaml + no_login.yaml
commit 3:        feat(chat) cp 完整 4 个新模块（不动旧代码，安全网）
commit 4:        refactor(synthesizer) 切换 import + 薄壳化
commit 5:        test(chat) 新增 prompt_assembler / meta_contexts 测试 + 文档收尾
```

**风险预案**（§4.4 Stop-Loss）：
- commit 3 后跑测试失败：删除新建 4 个模块，回滚到 commit 2 后状态（不影响主分支）
- commit 4 切换失败：在 commit 3 + commit 2 基础上修补，或 cherry-pick 回滚 commit 4
- 测试发现旧行为改变：先 grep 影响面 + 补 mocking，再决定

## 8. §9.7 改前 5 问

| # | 自检 | 答案 |
|---|------|------|
| 1 | 是否新增跨模块 import？ | 新增 `services/chat/` 子目录；orchestrator 调原 service 不变；不引入新跨模块依赖 |
| 2 | 模块边界是否变模糊？ | 更清晰（orchestrator 仅调度、prompt_assembler 仅构造 prompt、stream_dispatcher 仅流控）|
| 3 | 是否先有 Protocol 再写实现？ | 范围 A 不引入新 Protocol；4 个新模块是 orchestration 拆解，YAGNI（一个实现就够）|
| 4 | 改动是否破坏其他模块接口签名？ | `Synthesizer.run_stream()` 签名完全保持；api/chat.py 不改 |
| 5 | 模块能独立单测？ | prompt_assembler 纯函数（无 I/O）+ 可以单测；stream_dispatcher 需要 mock Provider；orchestrator 集成测试 |

## 9. §9.8 八件套（模块交付）

| # | 交付物 | 完成方式 |
|---|--------|----------|
| 1 | 模块职责说明 | 本 ADR + commit message |
| 2 | 接口定义 | orchestrator 公开 `Synthesizer.run_stream()`；prompt_assembler / stream_dispatcher 公开函数（无 Protocol，YAGNI）|
| 3 | 输入/输出模型 | orchestrator.run_stream 仍返回 `Generator[Tuple[str, Any], None, None]`；与原签名一致 |
| 4 | ORM / 数据模型 | 无 ORM 改动 |
| 5 | 依赖关系图 | 本 ADR §5 列；不引入新依赖 |
| 6 | 调用流程 | 本 ADR §数据流 |
| 7 | 测试方案 | commit 5 新增 2 个测试文件 + 全量回归 |
| 8 | 已知限制 | 不抽 4 个跨模块 Prompt（intent/rerank/rewriter/guard）；留给后续 Sprint |

## 10. Sprint 4 启动前置

- ✅ Synthesizer 928 → 4 模块
- ✅ G5 部分关闭（2 YAML）+ G7 缓解（万能模块拆完）
- ⏸ S4 业务规则配置化（阈值 YAML）独立可并行启动（不同模块）
- ⏸ 跨模块 Prompt 抽取（intent / rerank / query_rewriter / guard）安排到后续 Sprint

