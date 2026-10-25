# Agent 记忆 / 上下文 / 多 Agent / 多轮对话 升级方案（V21 路线图）

> 2026-10-23 · 基于 V20 六亮点的下一步规划
> 原则不变：**先定义什么算对（评测先行）→ 确定性规则优先 → LLM 只做该做的事 → 每改动必跑回归**

---

## 0. 现状盘点（诚实的四象限）

| 能力域 | 已有 | 缺口 |
|---|---|---|
| **记忆** | Redis 会话热层（N 轮+TTL）／MySQL 全量历史／Checkpointer 图状态（V20 亮点1） | **跨会话长期记忆为零**——conversation_contexts 表设计了但零调用方；买家画像/历史诉求不沉淀 |
| **上下文** | 三级漏斗（截断+指代消解+结构化注入）／90s 预算／请求级 embedding 复用 | **无摘要压缩**——超长会话丢早期信息；token 无硬顶 |
| **多 Agent** | 单图编排（LangGraph 四节点）+ 三级路由 + 13 工具；OceanMate 有 AtoA 链式经验 | 意图域处理是"函数"不是"Agent"（无统一接口/无独立状态）；**多意图协作缺失**——V12 支持 top-K=2 但两个意图只走一个 handler |
| **多轮对话** | eval_multiturn 20 组评测（V20 亮点5）／rewrite 指代消解／会话历史 | **规则层多轮覆盖仅 35%**（评测实测）——短句全靠 LLM 兜底；会话级实体缓存没有（"它"解析不回来） |

---

## M1 · 记忆机制：三级记忆 + 画像卡（1–2 天）

### 设计
```
L1 工作记忆   当前会话窗口（Redis，最近 N 轮）          —— 已有
L2 会话记忆   LangGraph Checkpointer（图中断恢复）      —— V20 已做
L3 长期记忆   买家画像卡（conversation_contexts 表）     ←—— 本包补齐
```

### L3 画像卡（关键增量）
- **结构**：`{user_id, top_categories, last_order_no, total_refunds, preferences[], last_intent, updated_at}`
- **写入**（会话结束异步，done 事件后 fire-and-forget）：
  - 规则抽取优先（零成本）：订单号→last_order_no、意图→last_intent、品类→top_categories 计数
  - LLM 摘要可选（灰度开关 `ENABLE_PROFILE_LLM_SUMMARY`，默认关）
- **读取**：接线 `ENABLE_USER_CONTEXT_PRELOAD`（**灰度开关已存在，V15 就埋好了**）——画像卡进 prompt 结构化字段
- **存哪里**：conversation_contexts 表（正好把零调用方的空表接线，技术债清偿）

### 验收
- 同一 user 二次进线 → trace/日志可见画像卡注入；`pytest` 新增 4 用例（写/读/TTL/降级）
- **设计说明**：「三级记忆：窗口内工作记忆、图状态 Checkpointer、跨会话画像卡——每级存储介质和生命周期都不同，这是按访问模式分层的」

---

## M2 · 上下文压缩：滚动摘要 + 事实槽（2 天，依赖 M1 表接线）

### 设计（定稿方案，落地实现）
```
prompt 结构 = 事实槽（永不压缩） + 滚动摘要（压缩区） + 最近 K 轮原文（保指代） + 当前 query
```
- **事实槽抽取**：正则——订单号（ORDER_NO_RE 复用）、金额（¥?\d+）、诉求关键词（退款/换货/发票…）→ 永不进摘要区
- **触发**：Redis 计数累计 token 超阈值（`CONTEXT_COMPRESS_THRESHOLD`，默认 3000 token 估算 len/2）
- **摘要生成**：qwen-turbo 增量合并 `new_summary = f(old_summary, 滑出轮次)`，**异步**（asyncio.to_thread，不阻塞当前应答）；失败退回截断（压缩是优化项不是必选项）
- **存储**：conversation_contexts.summary 字段 + Redis 热层同步
- **验证四件套（不过不上线）**：
  1. 60 题回归集不退步
  2. 事实保持率：正则从原文抽订单号/金额，校验摘要中留存（自动化）
  3. token 净节省 > 摘要调用成本
  4. 摘要失败降级截断的演练用例

### 验收
- 新模块 `context_compressor.py` + 8 用例；评测报告三份（压缩前/后/失败降级）

---

## M3 · 多 Agent：专家显式化 + 多意图协作（2–3 天）

### 立场（先说不是什么）
**不做 Supervisor**。每次分派多一次 LLM 调用，成本延迟翻倍，而三级路由用便宜一个数量级的方式实现了分派。多 Agent 的正确打开方式是下面两步。

### 第一阶段：意图域专家 Agent 化（1 天）
- 定义统一 `AgentProtocol`：`run(state) -> AgentResult`（OceanMate 的 Protocol 强隔离经验直接复用）
- 4 个专家：PolicyAgent / ProductAgent / OrderAgent / RefundAgent——把现有 handler 逻辑包进 Agent 类（内部各自可挂工具子集、各自有独立可测状态）
- 图内按意图 dispatch 到专家（结构不变，接口显式化）——**这是"多 Agent"的诚实版本：不是营销词，是真实接口隔离**

### 第二阶段：多意图并行协作（1–2 天）
- 现状：V12 意图分类已支持 top-K=2（一句话两个意图），但下游只走 primary
- 方案：双意图 → **两个专家并行跑**（asyncio.gather）→ 结果合成器：若两专家结论一致直接出；冲突时按置信度 + 业务优先级（安全 > 资金 > 体验）择优；任一专家触发 escalate → 整体升级
- 典型场景（MT20 实测）：「ORDxxx 我要退款 货还没退回去 物流单帮我看看」→ RefundAgent + OrderAgent 并行

### 验收
- 多意图评测场景 5 组（新增 eval_multiturn_v2）；双专家协作 trace 可回放；冲突择优有测试
- **设计说明**：「多意图一句话是电商客服的真实高频场景——两个专家并行而不是让一个 Agent 做两次决策，延迟不叠加；冲突时按业务优先级裁决」

---

## M4 · 多轮对话强化：把 35% 的规则覆盖打上去（1–2 天）

### 三招（按性价比排序）

**第一招 · 会话实体缓存（半天，性价比最高）**
- Redis per-session 缓存 `last_sku / last_order_no / last_intent`（TTL 30min）
- 指代解析规则级完成：「它/那个/这款」→ 上一轮 SKU；「我那个订单」→ last_order_no
- **零 LLM 成本**，直接服务 rewrite 与工具调用

**第二招 · classify 吃 rewrite 结果（半天）**
- 核查链路：当前 rewrite 只服务检索还是也喂了 classify？若 classify 在 rewrite 之前 → 调整顺序，多轮短句补全后再过规则层
- 预期：规则覆盖率 35% → 显著上升（真实数字跑出来）

**第三招 · 规则库多轮模式补强（半天）**
- 把 eval_multiturn 失败轮的高频模式（"便宜/拍下/补货/换货"等议价与交易动词）补进 intent.yaml
- **纪律**：补模式必须跑回归——新模式不得让既有 60 题 + 330 题 hit@k 退化（防词表通胀，P1-5 修过这个坑）

### 验收
- eval_multiturn 规则覆盖 35% → **目标 60%+**（目标值写死，跑出来是多少报多少）
- 每次规则改动：60 题 + 330 题 + 20 组多轮三套回归全绿

---

## 执行顺序与依赖

```
Day 1   M4-1 会话实体缓存（独立，立刻见效）
Day 1   M4-2 classify/rewrite 顺序核查
Day 2   M1 画像卡（表接线 + 预加载）
Day 3   M2 上下文压缩（依赖 M1）
Day 4   M3-1 专家 Agent 显式化
Day 5   M3-2 多意图并行协作
持续     M4-3 规则补强（随评测发现随补）
```

## 明确不做（诚实边界）

- ❌ Supervisor 全局编排（成本不划算，三级路由已覆盖）
- ❌ 向量库长期记忆（用户量级不需要，画像卡结构化字段够用）
- ❌ Agent 自由对话 / 自主规划（客服场景要确定性，规划=不可控）
- ❌ LLM 记忆抽取强制化（规则优先，LLM 摘要灰度）

## 总工作量：5–7 天；全部完成后的项目文档一句话

> 「Agent 记忆分三级（工作/会话/长期画像卡），上下文用事实槽+滚动摘要压缩（事实保持率自动校验），意图域 4 个专家 Agent 显式隔离 + 多意图双专家并行协作，多轮规则覆盖率从 35% 提到 60%+（评测驱动）。」
