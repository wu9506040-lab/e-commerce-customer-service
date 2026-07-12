# 项目开发规范优化方案（AI 编程治理 V1）

> 文档代号：GOV-V1 (Governance V1)
> 文档版本：V0.1（初版，待 Tech Lead review）
> 文档状态：🚧 待评审
> 输入来源：
>   - 现有 CLAUDE.md（443 行）
>   - 业务架构基线 `docs/business_architecture_v3.md`
>   - 开发计划 `docs/dev_plan_v1.md`
>   - 实际项目扫描报告（2026-07-11）
> 维护者：Tech Lead + 架构治理负责人
> 最近更新：2026-07-11

---

## 0. 文档元信息

| 项 | 值 |
|---|---|
| 优化目标 | 让 AI 编程工具（Claude Code 等）长期稳定开发，不破坏系统架构 |
| 优化对象 | CLAUDE.md / docs/ 目录 / 架构约束可执行性 / AI 任务执行流程 |
| 不在范围 | 业务架构重构 / 代码改造 / 多租户实际实现 |
| 落地方式 | 治理方案 + 完整草稿 + 分步执行建议 |

---

## 1. 现状诊断

### 1.1 CLAUDE.md 体检（443 行）

| § | 标题 | 行数 | 性质 | 适配度 |
|---|------|------|------|--------|
| §1 | 当前系统架构 | 9 | 业务介绍（变化快）| ❌ 不应放 CLAUDE.md |
| §2 | 禁止行为 | 7 | 永久不可违反 | ✅ 保留 |
| §3 | 开发原则 | 6 | 抽象原则 | 🟡 部分移到 docs/ |
| §4 | 工作流 | 9 | 流程（待重写为 AI 5 步法）| ⚠️ 重写 |
| §5 | Scope Lock | 7 | 单模块约束 | ⚠️ 优化 |
| §6 | 代码结构规范 | 25 | 分层规则 + 模块清单 | ✅ 保留（精简） |
| §7 | 项目过程记录 | 45 | 流程规范 | 🟡 移到 docs/ |
| §8 | 架构设计要求 | 320 | 18 条架构铁律（核心）| ✅ 保留（精简） |
| §9 | 与 V3 衔接 | 7 | 业务引用 | ❌ 移到 docs/ |

**核心问题：**

| # | 问题 | 影响 |
|---|------|------|
| P1 | CLAUDE.md 与 docs/ 职责不清 | AI 不知道哪些是"必须遵守"哪些是"参考" |
| P2 | 架构约束偏概念化 | "高内聚低耦合" 对 AI 无指导意义 |
| P3 | 无 AI 任务执行模板 | AI 不知道按什么流程接任务 |
| P4 | 缺乏反例 / 正例对照 | AI 容易"自创架构" |
| P5 | 业务介绍占行数 | 业务变化时需频繁改 CLAUDE.md |

### 1.2 docs/ 体检

```
docs/                                  行数   状态
├── PROJECT_DESIGN.md                  370    草稿（10 项 TODO 未填）
├── business_architecture_v3.md       720    V3.1 已冻结
├── dev_plan_v1.md                    587    DP-V1 初版
├── learning_log.md                  3021    M1-M13 演进记录
├── OPERATIONS.md                     217    运维指南
├── HEALTHCHECK.md                    163    healthcheck.io 接入
├── demo_walkthrough_report.md        142    M13 公网演示
├── test_coverage.md                   —    测试覆盖说明
├── refund_graph_v3.png                —    退款流程图
├── ecommerce_kb/                      —    知识库数据（gitignore）
└── _private/                          —    简历素材（gitignore）
```

**核心问题：**

| # | 问题 | 影响 |
|---|------|------|
| D1 | 文档全部平铺 | 10+ 个文件混杂，无清晰分类 |
| D2 | 无 docs README | 新人 / AI 不知道从哪看起 |
| D3 | 命名风格不统一 | `OPERATIONS.md` vs `HEALTHCHECK.md` vs `demo_walkthrough_report.md` |
| D4 | 缺治理类文档 | AI 开发规则 / 架构决策记录没有专门位置 |

### 1.3 AI 编程适配度评分

| 维度 | 当前 | 目标 | 差距 |
|------|------|------|------|
| **规则明确性** | 🟠 概念多、反例少 | 反例+正例对照 | 大 |
| **任务流程** | 🔴 无模板 | 5 步法强制 | 极大 |
| **架构可执行性** | 🟠 抽象原则 | 强制规则 + 反例 | 大 |
| **职责清晰度** | 🟡 CLAUDE.md 混杂 | CLAUDE.md 只放永久规则 | 中 |
| **文档组织** | 🟠 平铺 | 子目录分类 | 中 |
| **模块规范** | 🔴 无 README 要求 | 每个模块必带 README | 极大 |

---

## 2. CLAUDE.md 重新划分

### 2.1 内容归属决策矩阵

| 当前 § | 内容摘要 | 调整方向 | 原因 |
|--------|----------|----------|------|
| §1 当前系统架构 | 组件选型表格 | **移到** `docs/architecture/system.md` | 业务介绍，1 年后可能变 |
| §2 禁止行为 | 禁止 Kafka/微服务/拆分 | **保留** | 永久不可违反 |
| §3 开发原则 | 最小修改、不重构无关代码 | **保留**（精简为 3-4 条）| 永久原则 |
| §4 工作流 | Explore → Plan → Implement → Test | **重写**为 AI 5 步法 | 必须含 Step 3 等待确认 |
| §5 Scope Lock | 单模块修改 | **优化**为可执行规则 | 当前偏严，需要例外路径 |
| §6 代码结构规范 | 7 层架构 + 分层规则 | **保留**（精简）| 架构约束永久有效 |
| §7 项目过程记录 | learning_log 规范 | **移到** `docs/governance/learning_log_spec.md` | 流程规范不是强制规则 |
| §8 架构设计要求 | 18 条架构铁律 | **保留**（精简为反例+正例对照）| 核心永久规则 |
| §9 与 V3 衔接 | 业务架构引用 | **移到** `docs/architecture/business.md` | 业务引用 |

### 2.2 新 CLAUDE.md 结构（目标 200 行以内）

```
# 电商智能客服 Agent 系统 — AI 开发强制规则

> 永久不可违反的工程纪律 | 适用于 AI 编程工具 + 人工协作

## 1. 项目身份（10 行内）
一句话定位 + 技术栈表格

## 2. 禁止行为（永久）     [原 §2]
Kafka / 微服务 / 拆分 / 删文件 / 改密钥

## 3. AI 任务 5 步法（强制） [原 §4 重写]
Step 1 任务分析
Step 2 实施方案
Step 3 等待确认
Step 4 开发完成
Step 5 提交归档

## 4. Scope Lock（默认单模块）  [原 §5 优化]
默认单模块 + 跨模块例外路径

## 5. 架构铁律（强制规则）    [原 §8 精简]
5.1 三大铁律
5.2 强制规则（反例 + 正例）
5.3 自检 5 问
5.4 模块交付 8 件套

## 6. 单体架构约束          [新增]
禁止微服务；事件驱动限制

## 7. 多租户策略（MVP 阶段）  [新增]
tenant_id 默认值；不提前大规模重构

## 8. 参考文档              [新增]
docs/ 索引（指向 governance/architecture/operations/）
```

**预计行数：** ~200 行（vs 当前 443 行，↓55%）

### 2.3 CLAUDE.md 完整重写草稿

见 **附录 A**。

---

## 3. AI 开发强制规则（核心产出）

> 这是本次优化最重要的输出，AI 每次开发前必读。

### 3.1 三大铁律（AI 可执行版）

#### Interface First（必须）

**禁止：**

```python
# ❌ 错误：Agent 直接 import Order 具体类
from app.services.order_service import OrderService
order = OrderService.get_order_detail(order_no)
```

**必须：**

```python
# ✅ 正确：Agent 通过 Protocol 依赖
from app.services.protocols import OrderServiceProtocol
from app.deps import get_order_service

order_service: OrderServiceProtocol = get_order_service()
order = order_service.get_order_detail(order_no)
```

**AI 检查项（每次写 import 时）：**

| 检查 | 通过 |
|------|------|
| 跨模块 import 具体类？| ❌ 改为 Protocol |
| 跨模块调用内部函数（`_xxx`）？| ❌ 改用 Public Interface |
| 跨模块访问对方 ORM 模型？| ❌ 改为 DTO |

#### Module Isolation（必须）

**禁止清单（13 条）：**

| # | 禁止 | 反例 | 正例 |
|---|------|------|------|
| 1 | Agent 直接访问数据库 | `db.query(Order).all()` | `order_service.list_my_orders(user_id)` |
| 2 | RAG 直接调用订单内部函数 | `OrderTool._order_to_dict(...)` | `order_service.search(...)` |
| 3 | Controller 直接操作 Repository | `repo.save(order)` | `order_service.create(...)` |
| 4 | Emotion 模块读会话 | `db.query(Message)` | `conversation_service.get_messages(sid)` |
| 5 | 业务代码直接调用 Qwen SDK | `dashscope.Generation.call(...)` | `llm_provider.chat(messages)` |
| 6 | 业务代码直接调用 Embedding SDK | `dashscope.TextEmbedding.call(...)` | `embedding_provider.embed_text(text)` |
| 7 | 业务代码直接调用 Qdrant 客户端 | `QdrantClient.search(...)` | `vector_store.search(query, top_k)` |
| 8 | service 互相直接实例化 | `OrderService().create()` | 通过 DI 注入 |
| 9 | 业务代码和基础设施代码混合 | service 文件里 import qdrant | service 只调 Protocol |
| 10 | Prompt 散落代码 | service 里 `f"你是客服..."` | `prompt_registry.get("agent.system")` |
| 11 | 配置硬编码 | `if score > 80:` | `if score > config.threshold:` |
| 12 | 跨模块事件用直接调用 | service A 直接调 service B | 通过 EventBus 发事件 |
| 13 | 修改其他模块内部逻辑 | A 改 B 的私有函数 | A 通过 B 的扩展点 |

**AI 检查项（每次写代码后）：**

```bash
# 13 条反例自检脚本（建议加入 CI）
grep -rE "from app\.(services|clients|core)\.\w+ import \w+Service|Class" backend/app/
grep -rE "dashscope\.|qdrant_client\." backend/app/services/
grep -rE "db\.query\|session\.query" backend/app/services/
```

#### Dependency Inversion（必须）

**禁止：**

```python
# ❌ 高层直接依赖低层
class ChatService:
    def __init__(self):
        self.qwen = QwenClient()  # 直接实例化具体类
        self.qdrant = QdrantClient()
```

**必须：**

```python
# ✅ 高层依赖 Protocol
class ChatService:
    def __init__(
        self,
        llm_provider: LLMProvider,      # Protocol
        vector_store: VectorStore,      # Protocol
    ):
        self.llm = llm_provider
        self.vector = vector_store
```

### 3.2 AI 安全 5 防（独立模块）

| # | 防 | 触发场景 | Protocol |
|---|----|----------|----------|
| 1 | 防幻觉 | LLM 输出商品/订单/政策 | `HallucinationChecker` |
| 2 | 防承诺 | LLM 涉及退款/赔偿 | `PromiseChecker` |
| 3 | 防越权 | LLM 调 Tool 前 | `PermissionChecker` |
| 4 | 防敏感 | LLM 输出前 | `SensitiveChecker` |
| 5 | 防情绪升级 | 用户愤怒值 ≥ 80 | `EmotionChecker` |

**AI 调用规则：**

```python
# ✅ 5 防串联为 Pipeline
safety_pipeline = SafetyPipeline([
    hallucination_checker,
    promise_checker,
    permission_checker,
    sensitive_checker,
    emotion_checker,
])
result = safety_pipeline.check(llm_output, context)
if not result.passed:
    return fallback_answer()
```

### 3.3 Prompt 工程强制规则

**禁止：**

```python
# ❌ Prompt 硬编码在业务代码
SYSTEM_PROMPT = """你是电商客服..."""  # 出现在 service 文件
def build_prompt():
    return f"你是客服，请回答：{query}"
```

**必须：**

```python
# ✅ Prompt 来自 PromptRegistry
from app.core.prompts import get_prompt_registry

registry = get_prompt_registry()
system_prompt = registry.get("agent.system", tenant_id="default", version="v1")
```

**AI 检查项：**

```bash
# 禁止 Prompt 散落（CI 必跑）
grep -rE '"""[^"]*你是.*客服|f"[^"]*你是一名' backend/app/ --include="*.py"
```

### 3.4 多租户策略（MVP 阶段）

| 项 | 决策 | 原因 |
|----|------|------|
| tenant_id 默认值 | `"default"` | MVP 阶段所有数据属于默认租户 |
| tenant_id 字段 | **必须** 在所有新 ORM 模型 | 第一阶段就要带 |
| tenant_id 过滤 | **必须** 在所有 query | 防止数据泄露 |
| 隔离策略升级 | ❌ 不提前 | 等大客户触发 |
| Schema 隔离 | ❌ 不提前 | 同上 |
| DB 实例隔离 | ❌ 不提前 | 同上 |

**禁止：**

- ❌ 为了"未来可能多租户"提前大规模重构
- ❌ 设计多租户隔离策略（Schema/DB）而不触发
- ❌ 引入多租户 SaaS 框架（如 django-tenants）

### 3.5 单体架构约束

| 允许 | 禁止 |
|------|------|
| FastAPI 单体 | ❌ 微服务拆分 |
| 进程内 EventBus | ❌ Kafka / RabbitMQ / Redis MQ 复杂模式 |
| 异步任务（asyncio）| ❌ Celery / Dramatiq 等独立 worker 框架 |
| 模块化（Protocol 隔离）| ❌ 独立部署单元 |
| 单一 MySQL / Qdrant / Redis | ❌ 分库分表（除非数据量触发）|

**事件驱动实现：**

```python
# ✅ 允许：进程内 EventBus（asyncio / 简单 Pub-Sub）
event_bus.emit("high_risk_conversation", payload)

# ❌ 禁止：引入复杂 MQ
# from kafka import KafkaProducer  # NO
```

### 3.6 AI 自检 5 问（每次开发完成必过）

```markdown
## AI 自检清单（必须全过）

□ Q1：是否新增了"模块 A 直接 import 模块 B 的具体类"？→ 应改为依赖抽象
□ Q2：模块边界是否变模糊？→ 重构接口让改动收敛
□ Q3：是否先有 Protocol 再写实现？→ 必须先有 Protocol
□ Q4：改动是否破坏了其他模块依赖的接口签名？→ 同步更新接口 + 依赖方
□ Q5：这个模块能脱离其他模块单独跑测试吗？→ 不能说明耦合度过高
```

---

## 4. Scope Lock 优化

### 4.1 默认规则

```
默认：单模块修改
例外：跨模块必须走 §4.2 例外路径
```

### 4.2 跨模块例外路径

**触发条件：** 业务需求必然涉及 ≥2 个模块的接口变化。

**必须在 Step 2 实施方案中明确：**

| 项 | 内容 |
|----|------|
| 1. 为什么需要跨模块 | 业务原因（不是"为了架构漂亮"）|
| 2. 修改哪些接口 | 接口签名变化清单 |
| 3. 影响哪些模块 | 影响范围 + 调用方 |
| 4. 如何保持隔离 | 用 Protocol 隔离的方案 |

**示例（合规）：**

```markdown
## 跨模块改动：新增"订单备注"功能

1. 为什么：用户要求客服可给订单添加备注（业务需求）
2. 修改接口：
   - OrderServiceProtocol 新增 add_note(order_id, note)
   - Order 模型新增 notes JSON 字段
3. 影响模块：
   - OrderService（实现 add_note）
   - shop.py router（POST /orders/{no}/notes）
   - frontend OrderCard.vue（显示备注）
4. 如何隔离：
   - OrderServiceProtocol 扩展（向后兼容）
   - 其他模块无感（不依赖 add_note）
```

**反例（不合规）：**

```markdown
## ❌ 跨模块改动：重构 OrderTool 让所有 service 改用

1. 为什么：架构漂亮 → ❌ 不通过（不是业务原因）
2. 修改接口：所有 service 都要改
3. 影响模块：15+ 个
4. 如何隔离：无法隔离

→ 应该改为：保留 OrderTool，在 OrderTool 内部加 Protocol 包装
```

### 4.3 跨模块决策树

```
是否真的需要跨模块？
├─ 是：业务需求必然跨多个模块 → 走 §4.2 例外路径
├─ 否：可以在单个模块内完成 → 单模块
└─ 不确定：是否可以通过新增 Interface 让原模块 0 改动？
    └─ 优先选这个（符合 Dependency Inversion）
```

### 4.4 与单模块修改的边界

| 改动类型 | 是否算跨模块 | 处理 |
|----------|--------------|------|
| 新增字段（不影响接口）| ❌ 否 | 单模块 |
| 新增方法（不影响接口）| ❌ 否 | 单模块 |
| 接口签名变化（影响调用方）| ✅ 是 | 跨模块流程 |
| 新增 Protocol（新能力）| ❌ 否 | 单模块 |
| 新增依赖其他 service | 🟡 视情况 | 看是否破坏隔离 |
| 删字段 / 删方法 | ✅ 是 | 跨模块流程（需迁移）|

---

## 5. AI 任务执行模板（5 步法 · 强制）

> 任何 AI 开发任务（不论大小）必须按此流程执行。

### Step 1 任务分析（必输出）

```markdown
## 任务分析

### 当前代码情况
- 涉及文件：[file1, file2, ...]
- 现有接口：[列出相关 Protocol / 函数签名]
- 数据模型：[相关 ORM 模型]

### 涉及模块
- 主模块：[service/router 名称]
- 依赖模块：[列出]
- 被依赖模块：[列出]

### 影响范围
- 直接影响：[哪些 service / router / test]
- 间接影响：[通过 Protocol 隔离的模块不受影响]
- 性能影响：[预估]
- 数据影响：[是否需要迁移]

### 自检 5 问初步判断
- Q1 跨模块 import 具体类？[yes/no + 证据]
- Q2 模块边界变模糊？[yes/no]
- Q3 先有 Protocol 再实现？[yes/no]
- Q4 接口签名变化？[yes/no]
- Q5 模块可独立测试？[yes/no]
```

### Step 2 实施方案（必输出）

```markdown
## 实施方案

### 修改文件清单
| 文件 | 操作 | 说明 |
|------|------|------|
| app/services/protocols.py | 新增 | 新增 XxxProtocol |
| app/services/order_service.py | 修改 | 实现新接口 |
| app/api/shop.py | 修改 | 加新 endpoint |
| tests/test_xxx.py | 新增 | 单元测试 |

### 新增接口
```python
class OrderServiceProtocol(Protocol):
    def add_note(self, order_id: int, note: str) -> Order: ...
```

### 数据变化
- [ ] 不需要迁移
- [x] 需要迁移：`migrations/002_add_order_notes.sql`

### 风险评估
| 风险 | 等级 | 缓解 |
|------|------|------|
| 影响其他调用方 | 中 | 向后兼容 + 全量回归测试 |

### 测试策略
- [x] 单元测试（mock 依赖）
- [x] 接口测试（端到端）
- [x] 异常测试（边界场景）
```

### Step 3 等待确认（强制）

| 改动规模 | 是否需要确认 |
|----------|--------------|
| **大型修改**（≥3 个文件 或 接口签名变化 或 数据迁移）| ✅ **必须等用户确认** |
| **中型修改**（2-3 个文件，行为不变）| 🟡 建议确认 |
| **小型修改**（≤2 个文件，纯加字段/方法）| ❌ 可自动执行 |

**确认话术：**

```
"以上是实施方案，是否开始执行？

如确认，请回复：
- '确认执行' / 'go' / 'ok' 开始

如有调整，请直接指出修改点。"
```

### Step 4 开发完成（必输出）

```markdown
## 开发完成报告

### 修改文件列表
```
backend/app/services/protocols.py       +30 -0
backend/app/services/order_service.py   +20 -5
backend/app/api/shop.py                 +15 -0
backend/tests/test_order_service.py     +40 -0
```

### 测试结果
```
115 passed in 12.3s
+ 4 new tests for add_note functionality
```

### 架构影响（自检 5 问）
- Q1 ✅ 通过（用 Protocol 隔离）
- Q2 ✅ 通过（OrderService 边界清晰）
- Q3 ✅ 通过（Protocol 先于实现）
- Q4 ✅ 通过（新增方法，向后兼容）
- Q5 ✅ 通过（mock 后可独立测试）

### 后续建议
- 建议补 frontend OrderCard.vue 显示备注（下一步 sprint）
- 建议加 metrics 指标 add_note_called_count
```

### Step 5 提交归档

```bash
# 1. 更新 learning_log.md（按 §7 规范）
docs/learning_log.md  # 新增模块章节

# 2. git commit（按 git workflow 规范）
git add backend/app/services/protocols.py ...
git commit -m "feat(order): 新增订单备注功能 + Protocol 隔离"

# 3. 推送（如需要）
git push origin main
```

### 完整 5 步流程图

```
[用户发起任务]
    ↓
[AI 执行 Step 1 任务分析]
    ↓
[AI 执行 Step 2 实施方案]
    ↓
[Step 3 等待确认]
    ├─ 大型修改 → 必须确认
    ├─ 中型修改 → 建议确认
    └─ 小型修改 → 可自动
    ↓
[确认后执行]
    ↓
[AI 执行 Step 4 开发完成报告]
    ↓
[AI 执行 Step 5 提交归档]
    ↓
[任务结束]
```

---

## 6. 架构冲突检查

### 6.1 单体 vs 事件驱动（冲突分析）

| 维度 | 单体要求 | 事件驱动需求 | 解决方案 |
|------|----------|--------------|----------|
| 服务调用 | 直接调用 | 异步解耦 | **首选 Protocol 直接调用，必要时进程内 EventBus** |
| 数据一致性 | 强一致（同进程同事务）| 最终一致 | 单体保持强一致，事件仅用于跨模块通知 |
| 部署 | 单一进程 | 分布式 | 保持单一进程 |
| 扩展 | 横向加机器 | 纵向拆服务 | 横向扩展 |

**结论：**

- ✅ **允许：** 进程内 EventBus（asyncio / 简单 Pub-Sub）
- ✅ **允许：** 领域事件思想（Domain Event）
- ❌ **禁止：** Kafka / RabbitMQ / Redis Streams（用作消息队列）
- ❌ **禁止：** Celery / Dramatiq 等独立 worker
- ❌ **禁止：** 分布式事务 / Saga 模式

### 6.2 多租户 vs MVP（冲突分析）

| 维度 | BA-V3.1 要求 | MVP 现实 | 解决方案 |
|------|-------------|----------|----------|
| 多租户 | M17 完整多租户 | 当前单租户 | **MVP 阶段只加 tenant_id 字段，不做隔离策略** |
| 数据隔离 | Schema / DB 隔离 | 共享 DB | **共享 DB + tenant_id 过滤** |
| 路由 | 按租户分流 | 全局 | **JWT 注入 tenant_id，所有 query 自动过滤** |

**结论：**

- ✅ **必须：** 所有 ORM 表带 `tenant_id` 字段（默认 `"default"`）
- ✅ **必须：** 所有 query 带 tenant 过滤（middleware 自动注入）
- ❌ **禁止：** 提前实现 Schema 隔离
- ❌ **禁止：** 引入多租户框架
- ❌ **禁止：** 为"未来大客户"提前重构

### 6.3 AI Provider 抽象 vs 实际项目（冲突分析）

| 维度 | 抽象原则 | 实际项目 | 解决方案 |
|------|----------|----------|----------|
| LLM | 可替换 Provider | 当前只用 Qwen | **Sprint 1-2 抽 Protocol，保留 QwenProvider 实现** |
| Embedding | 可替换 Provider | 当前只用 DashScope | 同上 |
| Prompt | 独立管理 | 当前硬编码 | **Sprint 4 独立化，但优先保证行为不变** |

**结论：**

- ✅ **必须：** 抽 Protocol（即使只有一个实现）
- ✅ **必须：** 配置切换开关
- ❌ **禁止：** 为"未来可能用 GPT"提前实现多个 Provider
- ❌ **禁止：** 为"未来切换"做无谓的兼容性代码

---

## 7. 模块开发规范

### 7.1 module README（强制）

**每个模块目录必须有 `README.md`，内容如下：**

```markdown
# 模块名（如 OrderService）

## 模块职责
- 业务边界：订单的 CRUD 和状态管理
- 数据责任：orders / order_items 两张表

## 对外接口

### Protocol
```python
class OrderServiceProtocol(Protocol):
    def list_my_orders(self, user_id: int) -> list[Order]: ...
    def get_order_detail(self, order_no: str) -> Order: ...
    def create_order(self, user_id: int, items: list[CartItem]) -> Order: ...
```

## 输入输出

### Pydantic Schema
```python
class CreateOrderRequest(BaseModel):
    items: list[CartItem]
    address_id: int

class CreateOrderResponse(BaseModel):
    order_no: str
    total_amount: Decimal
```

## 数据模型
- `Order`（orders 表）：订单主表
- `OrderItem`（order_items 表）：订单明细

## 依赖关系

### 上游（本模块被谁调用）
- `api/shop.py` - 商品/订单路由
- `services/synthesizer.py` - Agent 决策

### 下游（本模块依赖谁）
- `models/order.py` - ORM 模型
- `services/user_service.py` - 用户信息（via Protocol）

### 禁止依赖（明确不能 import 哪些）
- ❌ `clients/qdrant.py` - 不需要向量检索
- ❌ `services/llm_provider.py` - 不调 LLM

## 调用流程

### 典型场景：用户下单
```
shop.py::create_order
    ↓
order_service.create_order
    ↓ (via Protocol)
order_lifecycle.create_order
    ↓
mysql_store.persist
```

## 测试方案
- 单元测试：`tests/unit/test_order_service.py`
- 集成测试：`tests/integration/test_shop_api.py`
- 异常测试：库存不足 / 地址不存在 / 支付失败

## 已知限制
- 单订单最多 50 个 SKU（性能限制）
- 暂不支持拆单
```

### 7.2 测试要求（强制）

**每个模块必须包含 3 类测试：**

| 测试类型 | 文件位置 | 覆盖率要求 |
|----------|----------|-----------|
| 单元测试 | `tests/unit/test_<module>.py` | ≥ 80% |
| 接口测试 | `tests/integration/test_<module>_api.py` | 100% endpoint |
| 异常测试 | 包含在单元测试中 | 必覆盖异常路径 |

**测试模板：**

```python
# tests/unit/test_order_service.py

class TestOrderService:
    """单元测试：mock 所有依赖"""

    def test_create_order_success(self):
        # 准备 mock
        mock_db = Mock()
        order_service = OrderService(db=mock_db)
        
        # 执行
        result = order_service.create_order(user_id=1, items=[...])
        
        # 验证
        assert result.order_no is not None
        mock_db.add.assert_called_once()

    def test_create_order_with_invalid_items(self):
        """异常测试：空 items"""
        with pytest.raises(ValueError, match="items 不能为空"):
            order_service.create_order(user_id=1, items=[])
```

### 7.3 模块交付 8 件套（开发完成必提交）

| # | 交付物 | 说明 |
|---|--------|------|
| 1 | 模块 README.md | 按 §7.1 模板 |
| 2 | Protocol 定义 | `app/services/protocols.py` |
| 3 | 实现代码 | `app/services/<module>.py` |
| 4 | Pydantic Schema | `app/schemas/<module>.py` |
| 5 | ORM 模型 | `app/models/<module>.py`（如有）|
| 6 | 单元测试 | `tests/unit/test_<module>.py` |
| 7 | 集成测试 | `tests/integration/test_<module>_api.py` |
| 8 | learning_log 更新 | 新增章节 |

---

## 8. docs 目录规划

### 8.1 当前结构（平铺）

```
docs/
├── PROJECT_DESIGN.md
├── business_architecture_v3.md
├── dev_plan_v1.md
├── learning_log.md
├── OPERATIONS.md
├── HEALTHCHECK.md
├── demo_walkthrough_report.md
├── test_coverage.md
├── refund_graph_v3.png
├── ecommerce_kb/    (gitignore)
└── _private/        (gitignore)
```

### 8.2 目标结构（子目录分类）

```
docs/
├── README.md                              # docs 索引（新）
│
├── architecture/                          # 架构类
│   ├── README.md                          # 架构类索引
│   ├── business.md                        # 从 business_architecture_v3.md 重命名
│   ├── development_plan.md                # 从 dev_plan_v1.md 重命名
│   └── system.md                          # 从 CLAUDE.md §1 移出（新）
│
├── governance/                            # 治理类（新）
│   ├── README.md
│   ├── ai_development_rules.md            # 本文档核心产出（新）
│   ├── claude_md_optimization.md          # 本文档（新）
│   └── learning_log_spec.md               # 从 CLAUDE.md §7 移出（新）
│
├── operations/                            # 运维类
│   ├── README.md
│   ├── deployment.md                      # 从 OPERATIONS.md 重命名
│   ├── healthcheck.md                     # 从 HEALTHCHECK.md 重命名
│   └── monitoring.md                      # 新（监控指标说明）
│
├── design/                                # 设计类
│   ├── README.md
│   ├── project_design.md                  # 从 PROJECT_DESIGN.md 重命名
│   └── refund_graph_v3.png                # 保留
│
├── reports/                               # 报告类
│   ├── README.md
│   ├── demo_walkthrough.md                # 从 demo_walkthrough_report.md 重命名
│   └── test_coverage.md                   # 保留
│
├── learning_log.md                        # 保留（M1-M13 演进）
│
├── ecommerce_kb/                          # 保留（gitignore）
└── _private/                              # 保留（gitignore）
```

### 8.3 命名规范

| 类型 | 规范 | 示例 |
|------|------|------|
| 架构文档 | `<topic>.md` | `business.md` / `development_plan.md` |
| 治理文档 | `<topic>.md` | `ai_development_rules.md` |
| 运维文档 | `<topic>.md` | `deployment.md` / `healthcheck.md` |
| 设计文档 | `<topic>.md` | `project_design.md` |
| 报告文档 | `<topic>.md` | `demo_walkthrough.md` |
| 索引文档 | `README.md` | 每个子目录一个 |

### 8.4 docs 索引（docs/README.md）

```markdown
# 项目文档索引

## 按角色看

### 产品 / 业务方
- [业务架构](architecture/business.md) - 产品定位 / 业务能力 / KPI
- [项目设计](design/project_design.md) - 项目愿景 / 核心价值

### 开发工程师
- [AI 开发规则](governance/ai_development_rules.md) - **必读**
- [开发计划](architecture/development_plan.md) - Sprint 排期
- [架构设计要求](../CLAUDE.md §5) - 架构铁律

### 运维工程师
- [部署指南](operations/deployment.md)
- [监控配置](operations/monitoring.md)
- [健康检查](operations/healthcheck.md)

### 面试 / 简历
- [_private/resume_snippet.md](_private/resume_snippet.md)（不进 Git）

## 按类型看

- 架构类：[architecture/](architecture/)
- 治理类：[governance/](governance/)
- 运维类：[operations/](operations/)
- 设计类：[design/](design/)
- 报告类：[reports/](reports/)
- 演进记录：[learning_log.md](learning_log.md)
```

---

## 9. 修改建议列表（执行步骤）

### 9.1 P0 — 立即执行（1-2 天）

| # | 任务 | 工作量 | 风险 |
|---|------|--------|------|
| 1 | 新建 `docs/governance/` 子目录 | 0.5h | 0 |
| 2 | 落地本文档为 `docs/governance/ai_development_rules.md` | 1h | 0 |
| 3 | 落地 CLAUDE.md 重写版（按附录 A）| 2h | 中（规则变化） |
| 4 | 新建 `docs/README.md` 索引 | 1h | 0 |
| 5 | 移动 docs 子目录（architecture/governance/operations/design/reports）| 1h | 中（路径变化） |

**注意：** 路径变化需要同步更新 CLAUDE.md / README.md / 文档内引用。

### 9.2 P1 — 1 个月内

| # | 任务 | 工作量 |
|---|------|--------|
| 6 | 完善 `docs/architecture/system.md`（从 CLAUDE.md §1 移出）| 1h |
| 7 | 完善 `docs/governance/learning_log_spec.md` | 1h |
| 8 | 各模块补 README.md（Sprint 1 同步进行）| 每个模块 1h |
| 9 | CI 增加 §3.6 自检 5 问脚本（grep 反例）| 2h |
| 10 | CI 增加 §3.3 Prompt 散落检查 | 1h |

### 9.3 P2 — 按需

| # | 任务 | 触发 |
|---|------|------|
| 11 | 编写 `docs/operations/monitoring.md` | M15 启动 |
| 12 | 完善 `docs/design/project_design.md` 的 10 项 TODO | 业务访谈后 |
| 13 | 模块 README.md 全量覆盖 | Sprint 7+ |

### 9.4 不建议改动

| 项 | 原因 |
|----|------|
| ❌ 拆分 `learning_log.md`（3021 行）| 历史记录保留完整 |
| ❌ 重新组织 `ecommerce_kb/` | gitignore 内容 |
| ❌ 修改 `_private/resume_snippet.md` | 私人内容 |

---

## 10. 变更记录

| 版本 | 日期 | 作者 | 变更 |
|------|------|------|------|
| V0.1 | 2026-07-11 | Tech Lead + 架构治理负责人 | 初版（治理方案） |

---

## 附录 A：CLAUDE.md 完整重写草稿

```markdown
# 电商智能客服 Agent 系统 — AI 开发强制规则

> 永久不可违反的工程纪律
> 适用于 AI 编程工具（Claude Code / MiniMax 等）+ 人工协作
> 详细架构规划见 docs/architecture/

---

## 1. 项目身份

| 项 | 值 |
|---|---|
| 类型 | 企业级 AI 客服员工平台（FastAPI 单体）|
| 技术栈 | FastAPI + Vue3 + Qdrant + Redis + MySQL + Qwen |
| 部署 | Docker Compose → 阿里云 ECS 120.79.27.124 |
| 架构原则 | Interface First + Module Isolation + Dependency Inversion |
| 详细架构 | `docs/architecture/business.md` + `docs/architecture/system.md` |

---

## 2. 禁止行为（永久不可违反）

| # | 禁止行为 | 原因 |
|---|----------|------|
| 1 | ❌ 微服务拆分 / Kafka / MQ 引入 | 单体架构约束 |
| 2 | ❌ 跨模块直接 import 具体类 | 违反 Interface First |
| 3 | ❌ Controller 直接操作数据库 | 违反分层架构 |
| 4 | ❌ 业务代码直接调用第三方 SDK | 必须经过 Provider 抽象 |
| 5 | ❌ Prompt 硬编码在业务代码 | 必须独立管理 |
| 6 | ❌ 业务阈值/规则硬编码 | 必须配置化 |
| 7 | ❌ 删除文件（除非明确授权）| 风险控制 |
| 8 | ❌ 改密钥 / 数据库密码 | 安全控制 |
| 9 | ❌ 无验证提交 | 必须 curl / pytest 验证 |
| 10 | ❌ 乱装依赖 | package.json / requirements.txt 受控 |

---

## 3. AI 任务 5 步法（强制执行）

> 任何 AI 开发任务（不论大小）必须按此流程。

### Step 1 任务分析

输出：
- 当前代码情况（涉及文件 + 现有接口 + 数据模型）
- 涉及模块（主模块 + 依赖模块 + 被依赖模块）
- 影响范围（直接影响 + 间接影响 + 性能/数据影响）
- 自检 5 问初步判断

### Step 2 实施方案

输出：
- 修改文件清单
- 新增接口（含 Protocol 签名）
- 数据变化（Schema / 迁移）
- 风险评估
- 测试策略

### Step 3 等待确认

| 改动规模 | 是否确认 |
|----------|----------|
| 大型（≥3 文件 / 接口签名变化 / 数据迁移）| ✅ 必须确认 |
| 中型（2-3 文件，行为不变）| 🟡 建议确认 |
| 小型（≤2 文件，纯加字段/方法）| ❌ 可自动执行 |

确认话术：「以上是实施方案，是否开始执行？」

### Step 4 开发完成

输出：
- 修改文件列表（git diff stat）
- 测试结果（pytest 输出）
- 架构影响（自检 5 问全过）
- 后续建议

### Step 5 提交归档

- 更新 docs/learning_log.md
- git commit（按规范）
- 推送（如需要）

完整模板见 `docs/governance/ai_development_rules.md §5`。

---

## 4. Scope Lock（默认单模块）

### 默认
单模块修改。

### 跨模块例外
必须明确：
1. 为什么需要跨模块（业务原因）
2. 修改哪些接口
3. 影响哪些模块
4. 如何保持隔离

详见 `docs/governance/ai_development_rules.md §4`。

---

## 5. 架构铁律（强制规则）

### 5.1 三大铁律

| 原则 | 含义 | 落地 |
|------|------|------|
| **Interface First** | 先 Protocol 再实现 | 跨模块调用必须经过 Protocol |
| **Module Isolation** | 模块强隔离 | 13 条反例清单（见 §3.2）|
| **Dependency Inversion** | 依赖方向单向 | 高层依赖抽象，不依赖细节 |

### 5.2 AI 自检 5 问（每次开发完成必过）

```
□ Q1：是否新增了"模块 A 直接 import 模块 B 的具体类"？
□ Q2：模块边界是否变模糊？
□ Q3：是否先有 Protocol 再写实现？
□ Q4：改动是否破坏了其他模块依赖的接口签名？
□ Q5：这个模块能脱离其他模块单独跑测试吗？
```

任一项不通过 → 必须重构。

### 5.3 模块交付 8 件套

每个模块完成必须提交：

1. 模块 README.md
2. Protocol 定义
3. 实现代码
4. Pydantic Schema
5. ORM 模型（如有）
6. 单元测试（≥80% 覆盖）
7. 集成测试（100% endpoint）
8. learning_log 更新

详见 `docs/governance/ai_development_rules.md §7`。

### 5.4 单体架构约束

| 允许 | 禁止 |
|------|------|
| ✅ 进程内 EventBus | ❌ Kafka / RabbitMQ |
| ✅ asyncio 异步 | ❌ Celery / Dramatiq |
| ✅ 横向加机器 | ❌ 微服务拆分 |

---

## 6. 多租户策略（MVP 阶段）

| 项 | 决策 |
|----|------|
| tenant_id 字段 | ✅ 必须有（默认 `"default"`）|
| tenant_id 过滤 | ✅ 必须（middleware 自动注入）|
| Schema 隔离 | ❌ 不提前 |
| DB 实例隔离 | ❌ 不提前 |
| 多租户框架 | ❌ 禁止引入 |

**禁止：** 为"未来大客户"提前大规模重构。

---

## 7. 参考文档

| 类型 | 文档 |
|------|------|
| AI 开发规则 | `docs/governance/ai_development_rules.md` |
| 业务架构 | `docs/architecture/business.md` |
| 开发计划 | `docs/architecture/development_plan.md` |
| 系统架构 | `docs/architecture/system.md` |
| 运维指南 | `docs/operations/deployment.md` |
| 设计文档 | `docs/design/project_design.md` |
| 演进记录 | `docs/learning_log.md` |

---

> **版本：** V1.0（精简版）
> **变更：** 从 443 行精简到 ~200 行；业务介绍移到 docs/；新增 AI 5 步法
```

**预计行数：** ~200 行（vs 当前 443 行，↓55%）

---

## 附录 B：本文档落地建议

本文档（governance_v1.md）建议落地为 `docs/governance/ai_development_rules.md`，作为 AI 开发规则的权威来源。

落地步骤：

```bash
# 1. 创建 governance 子目录
mkdir -p docs/governance

# 2. 落地本文档
cp docs/governance_v1.md docs/governance/ai_development_rules.md

# 3. 后续按 §9 修改建议执行
```