# 电商智能客服 Agent 系统 — 项目总设计

> 协作设计文档，每完成一段就更新一次。
> 状态：🚧 草稿 / V2 迭代中

---

## 0. 文档元信息

| 项 | 值 |
|---|---|
| 项目代号 | e-commerce-cs-agent |
| 当前版本 | V2（设计阶段）|
| 最后更新 | 2026-06-26 |
| 状态 | 🚧 草稿 |
| 维护者 | zwyyy7 |

---

## 1. 项目愿景

### 一句话定位

> 基于 **RAG + Tool** 的电商智能客服系统，支持 **商品咨询 / 订单查询 / 售后规则** 自动化问答。

### 目标用户

- [x] C 端消费者（演示 / 自用）
- [ ] B 端商家（不做）
- [ ] 第三方开发者（仅 API）

### 三大核心能力

| # | 能力 | 数据来源 | 技术路径 | 阶段 |
|---|------|---------|---------|------|
| 1 | 🟢 商品咨询 | 商品标题 / 详情 / SKU 属性 | RAG（Qdrant）| M2 |
| 2 | 🔵 订单 / 物流查询 | MySQL orders / logistics | Tool（结构化查询）| M2 |
| 3 | 🟡 售后规则问答 | 政策文档 / FAQ | RAG + Tool 状态校验 | M2 |

> 注：用户评价（review_text）暂不入 RAG（噪音大），留独立字段，V3 再评估。

### 核心价值

| # | 价值 | 体现 |
|---|------|------|
| 1 | 降低客服人力 | 自动处理 70%+ 高频问题（商品参数 / 物流 / 退款规则）|
| 2 | 回答可追溯 | 每条回复附 KB 来源 / 订单号，可点击核验 |
| 3 | 快速接入新业务 | 数据 + 服务分层，加一类商品只需改 KB + 商品表 |

---

## 2. 系统架构

### 架构图

```
                     ┌────────────────────────────┐
                     │   浏览器（Vue3 + TS）       │
                     │   消费者                     │
                     └────────────┬───────────────┘
                                  │ HTTP / SSE
                                  ▼
        ┌──────────────────────────────────────────────────┐
        │            FastAPI Chat API                       │
        │            POST /chat (SSE)                        │
        └─────────────────────┬──────────────────────────┘
                              │
                       Intent Classifier
                       （规则优先 → LLM 兜底）
                              │
        ┌─────────────────────┼─────────────────────────────┐
        │                     │                             │
   product_query         order_query                 policy_query
        │                     │                             │
        ▼                     ▼                             ▼
 ┌──────────────┐    ┌──────────────────┐        ┌──────────────────┐
 │ Product      │    │  Order Service   │        │  Policy Service  │
 │ Service      │    │  (Tool Layer)    │        │  (RAG)           │
 │ (RAG)        │    │                  │        │                  │
 └──────┬───────┘    └────────┬─────────┘        └────────┬─────────┘
        │                     │                           │
        │ refund_query = 复合路径（tool + policy）         │
        │                     │                           │
        ▼                     ▼                           ▼
 ┌──────────────┐    ┌──────────────────┐        ┌──────────────────┐
 │  Qdrant      │    │  MySQL 8.0       │        │  Qdrant v1.10    │
 │  - 商品 KB   │    │  - orders        │        │  - 政策 KB       │
 │  - FAQ       │    │  - order_items   │        │  - FAQ           │
 └──────────────┘    │  - refunds       │        └──────────────────┘
                     │                  │
                     │  Redis 7         │
                     │  - session       │
                     │  - 限流          │
                     └──────────────────┘
                              │
                              ▼
                  ┌────────────────────────┐
                  │  Response Synthesizer  │
                  │  多源融合 + 单一 LLM    │
                  └────────────┬───────────┘
                               │
                       SSE 流式输出
```

### 端到端数据流（refund 复合路径举例）

```
用户：「我 3 天前买的耳机能退吗？」
   ↓
1. Intent Classifier → refund_query（匹配"买 + 退"）
   ↓
2. Order Tool：按 user_id 查该用户的近期订单 → 找到耳机订单
   ↓
3. Order Tool：检查 status（已发货?签收?天数?）
   ↓
4. Policy RAG：召回「七天无理由 / 已激活不支持退货」相关条款
   ↓
5. Response Synthesizer：
   - structured_data = {order: {...}, status: "shipped"}
   - policy_docs = ["7天无理由...", "已激活不支持..."]
   - prompt 模板硬约束：先陈述事实，再引政策，最后给结论
   ↓
6. LLM（qwen-max）生成 → SSE 流式输出
```

---

## 3. 模块划分

### 模块清单

| # | 模块 | 职责 | 状态 | 阶段 |
|---|------|------|------|------|
| 1 | Chat / SSE | 流式对话入口 | ✅ V1.2 | — |
| 2 | Auth / RBAC | JWT + bcrypt | ✅ V1.2 | — |
| 3 | Session | 会话管理（Redis）| ✅ V1.2 | — |
| 4 | RAG Pipeline | 检索 + 生成 | ✅ V1.2 | — |
| 5 | Knowledge Base | KB 管理 | ✅ V1.2 | — |
| 6 | **Intent Classifier** | 4 类意图分类（规则 + LLM 兜底）| 🚧 设计 | M3 |
| 7 | **Product Service** | 商品 RAG + 列表查询 | 🚧 设计 | M2 |
| 8 | **Order Service** | 订单结构化查询 | 🚧 设计 | M2 |
| 9 | **Refund Service** | 退款（tool + policy 融合）| 🚧 设计 | M2 |
| 10 | **Policy Service** | 政策 RAG | 🚧 设计 | M2 |
| 11 | **Tools Layer** | 函数调用层（非 agent）| 🚧 设计 | M2 |
| 12 | **Response Synthesizer** | 多源融合 + 单一 LLM | 🚧 设计 | M4 |
| 13 | Agent（多步推理）| 🚫 禁用 | — | V3+ |
| 14 | Admin Portal | 商家后台 | ⏸ 后置 | — |

### 模块依赖

```
Chat / SSE
  └── Intent Classifier
        ├── Product Service ── RAG Pipeline ── Qdrant
        ├── Order Service ─── Tools Layer ── MySQL
        ├── Refund Service ──┬── Tools Layer ── MySQL
        │                    └── Policy Service ── RAG Pipeline
        └── Policy Service ── RAG Pipeline ── Qdrant

Response Synthesizer ← 所有 Service 输出
Auth ── 所有受保护端点
Session ── Chat
```

### 不做的（YAGNI / Scope Lock）

| 项 | 不做的理由 |
|---|---|
| Agent 框架（LangGraph / 自研 base）| 单步任务可靠，多步易错（CLAUDE.md §2）|
| memory/ 独立层 | 现有 services/session.py + Redis 够用 |
| 多租户 | demo 单租户 |
| 支付 | D7 不做（仅客服）|
| 商家后台 | D6 不做 |
| Rerank | 52 KB 量级增益不抵 +200ms 成本 |
| 多轮指代消解 | V1.x 弱支持，V3 评估 |

---

## 4. 技术栈选型

| 层 | 选型 | 备注 |
|---|---|---|
| 前端 | Vue3 + Vite + TS | 已定 |
| 后端 | FastAPI + Python 3.11 | 单体（CLAUDE.md §2）|
| LLM | 通义千问 qwen-max | 主对话；classify 走规则不调 LLM |
| Embedding | DashScope text-embedding-v3 | 1024 维 |
| 向量库 | Qdrant v1.10 | COSINE |
| 关系库 | MySQL 8.0（utf8mb4）| 已定 |
| 缓存 | Redis 7 | session + 限流 |
| 反代 | nginx | — |
| 部署 | Docker Compose | 5 服务 |

### 选型原则（CLAUDE.md §2 强制）

- ❌ 禁 Kafka / Milvus / Elasticsearch / 新数据库
- ❌ 禁拆微服务（保持 FastAPI 单体）
- ❌ 禁 LangGraph / 过度抽象的 AI 框架
- ❌ 禁 Rerank（V2.x，预留接口，V3 评估）
- ✅ YAGNI / Scope Lock / 单模块推进
- ✅ Tool ≠ Agent（tool 是函数，agent 是规划器）

---

## 5. 数据模型

### ER 图

```
users ──┬── conversations ── messages
        │         │
        │         └── last_intent, current_order_id（多轮 slot 预留）
        │
        └── orders ── order_items ── products
                │           │
                │           └── product_categories
                │
                ├── shipping_addresses
                ├── logistics
                └── refunds

products ── product_categories（类目树）
```

### 表清单

| 表 | 关键字段 | 状态 | 说明 |
|---|---|---|---|
| `users` | id, email, password_hash | ✅ V1.2 | 用户主体 |
| `conversations` | id, user_id, title, last_intent | ✅ V1.2 | 会话索引 |
| `messages` | id, conv_id, role, content, sources | ✅ V1.2 | 消息明细 |
| `knowledge_documents` | id, source, doc_type, status | ✅ V1.2 | KB 元数据 |
| `operation_logs` | id, user_id, action, payload | ✅ V1.2 | 审计日志 |
| `products` | id, sku, name, description, price, category_id, attributes(JSON), review_text | 🚧 设计 | M1 |
| `product_categories` | id, name, parent_id | 🚧 设计 | M1 |
| `orders` | id, user_id, status, total_amount, address_id | 🚧 设计 | M1 |
| `order_items` | id, order_id, product_id, qty, unit_price | 🚧 设计 | M1 |
| `refunds` | id, order_id, reason, status, created_at | 🚧 设计 | M1 |

### products 字段约定

| 字段 | 类型 | 说明 |
|---|---|---|
| id | BIGINT PK | — |
| sku | VARCHAR(50) UNIQUE | 业务编号（SKU001-SKU010）|
| name | VARCHAR(200) | 商品名 |
| description | TEXT | 详情（入 RAG）|
| price | DECIMAL(10,2) | 售价 |
| category_id | BIGINT FK | 类目 |
| attributes | JSON | 颜色 / 尺码 / 规格 |
| review_text | TEXT | 用户评价（**不入 RAG**，独立字段）|
| stock | INT | 库存 |
| status | TINYINT | 0=下架 / 1=在售 |
| created_at, updated_at | DATETIME | 自动填充 |

### orders 状态机

```
pending → paid → shipped → delivered → completed
                ↘ refunded（全状态可发起退款申请）
```

| status | 含义 | 可执行操作 |
|---|---|---|
| pending | 待支付 | 取消 |
| paid | 已支付 | 取消 / 发货 |
| shipped | 已发货 | 拒收 / 物流查询 |
| delivered | 已签收 | 确认收货 / 7 天内可退 |
| completed | 已完成 | 仅售后 |
| refunded | 已退款 | 终态 |

---

## 6. API 设计

### 已实现（V1.2）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/auth/register` | 注册 |
| POST | `/auth/login` | 登录 |
| POST | `/chat` | RAG 流式问答 |
| POST | `/admin/ingest` | KB 入库 |

### 规划（M2–M4）

| 方法 | 路径 | 说明 | 阶段 | 鉴权 |
|---|---|---|---|---|
| POST | `/intent` | 意图分类（独立调试）| M3 | ✅ |
| GET | `/products` | 商品列表 / 搜索 | M2 | ❌ |
| GET | `/products/{sku}` | 商品详情 | M2 | ❌ |
| GET | `/orders` | 我的订单（按 JWT user_id）| M2 | ✅ |
| GET | `/orders/{order_id}` | 订单详情（仅本人）| M2 | ✅ |
| GET | `/orders/{order_id}/logistics` | 物流轨迹 | M2 | ✅ |
| POST | `/refunds` | 申请退款 | M2 | ✅ |
| GET | `/refunds/{refund_id}` | 退款详情 | M2 | ✅ |

### 鉴权与越权防护

| 规则 | 说明 |
|---|---|
| 订单查询不接受外部 order_id | 全部按 JWT user_id 自动查 |
| `/orders/{order_id}` 校验 owner | 非本人 403 |
| Tool 层强制注入 user_id | service 调用 tool 时强制传当前 user，service 不可信前端 |

---

## 7. 模型能力边界

### LLM 能做（强项）

| 能力 | 适用 | 备注 |
|---|---|---|
| 自然语言理解 | ✅ 用户问题理解 | 准确率 ~95% |
| 文本生成 | ✅ 客服回答 | 流畅自然 |
| 文档总结 | ✅ 长文档压缩 | 适合 FAQ 整理 |
| 多源融合 | ✅ Response Synthesizer | prompt 硬约束优先级 |
| 多轮对话 | ⚠️ 受 slot filling 限制 | V1.x 弱支持，预留 slot 字段 |

### LLM 不做（V2 明确禁用）

| 能力 | 现状 | 替代 |
|---|---|---|
| 精确计算 | ❌ 价格 / 折扣 | 程序逻辑 |
| 实时数据 | ❌ 库存 / 物流 | 查 MySQL |
| **意图分类（默认）** | ❌ LLM | **规则 + 正则**（更快更便宜）|
| 多步推理 / Agent | ❌ 禁用 | Tool 单步调用 |
| 跨会话记忆 | ❌ 不做 | MySQL / Redis 持久化 |
| Rerank | ❌ V2.x | V3 评估 |

### Intent Classifier 设计

| 优先级 | 方式 | 触发条件 |
|---|---|---|
| 1 | 关键词 + 正则 | 命中即返回，跳过 LLM |
| 2 | LLM 兜底（few-shot）| 规则未命中，调 qwen-turbo |
| 3 | 默认 → policy_query | 兜底兜底，避免空响应 |

```python
INTENT_RULES = [
    ("order_query",   [r"我的订单", r"物流", r"快递", r"发货", r"到哪"]),
    ("refund_query",  [r"退款", r"退货", r"退换", r"不想要了"]),
    ("product_query", [r"多少钱", r"参数", r"续航", r"颜色", r"尺码"]),
    # 默认 policy_query（兜底）
]
```

### 多源融合约束（Response Synthesizer）

| 优先级 | 来源 | 在 prompt 中的位置 |
|---|---|---|
| 1 | Tool 结构化数据 | 「事实陈述」（最高）|
| 2 | 用户订单上下文 | 「已知信息」|
| 3 | Policy RAG | 「政策依据」|
| 4 | Product RAG | 「商品知识」|

> 硬约束模板：先讲事实（订单状态），再引政策（条款编号），最后给结论（可执行操作）。

### 失败模式（容错策略）

| 场景 | 表现 | 系统应对 |
|---|---|---|
| 召回无相关 KB | 幻觉 | prompt 强制「我不知道」+ 引官方客服 |
| LLM 超时 | 504 | 重试 1 次 + 降级 FAQ 文本 |
| embedding API 挂 | 检索失败 | 缓存 + 离线 fallback |
| Tool 查询失败 | — | 返回「系统繁忙，请稍后再试」|
| Token 超限 | 中断 | 截断历史 + 滑动窗口 |

---

## 8. 开发路线图（里程碑制）

### 5 阶段里程碑

| 里程碑 | 内容 | 交付物 | 状态 |
|---|---|---|---|
| **M1** | 数据层 | products / orders / order_items / refunds 表 + mock 数据 + KB 扩展 | ✅ 完成（2026-06-27）|
| **M2** | 服务层 | Product / Order / Refund / Policy Service + Tools Layer | ✅ 完成（2026-06-27）|
| **M3** | 路由层 | Intent Classifier（规则优先）+ 独立 /intent 端点 | ✅ 完成（2026-06-27）|
| **M4** | 融合层 | Response Synthesizer + 端到端 SSE 集成到 /chat | ✅ 完成（2026-06-27）|
| **M5** | 验收 | 4 类意图各 10 条测试用例 + 浏览器联调 | ✅ 完成（2026-06-27，平均 95.8% 通过）|

### 现有基础（已交付）

- ✅ V1.2 通用智能客服（chat + auth + session + RAG + KB）
- ✅ 电商 KB 67 条 Qdrant 点（商品 10 / 政策 4 / FAQ 25 + 原 5）
- 🚧 MySQL 元数据 session bug（暂不修，Qdrant 主路径 OK）
- ✅ doc_type 字段区分 product / policy / faq

### 里程碑验证标准（每个 M 结束必须过）

| M | 验证项 | 方式 |
|---|---|---|
| M1 | 4 张表 + 10 商品 + 5 订单 mock | `SELECT COUNT(*)` + 随机抽查 |
| M2 | curl 4 类 service 端点返回正确 | `curl /products/SKU001`、`curl /orders` |
| M3 | `/intent` 对 10 条测试用例分类 | 准确率 ≥ 80% |
| M4 | SSE 端到端跑通 3 类意图 | latency < 3s 首 token |
| M5 | 4 类各 10 条 = 40 用例 | 通过率 ≥ 85% → **实测 95.8%（3 次平均）** ✅ |

---

## 9. 非功能性需求

### 性能

| 指标 | 目标 | 实测（2026-06-27）| 测量方式 |
|---|---|---|---|
| /chat 首 token | < 2s | **P50 = 651ms / P95 = 2081ms（边界）** | curl + 时间戳 |
| /chat 整体完成 | < 5s | **P50 = 2086ms / P95 = 5316ms（边界）** | 同上 |
| /intent 响应 | < 100ms | 纯规则 < 10ms ✅ | 纯规则，零 LLM |
| 并发用户 | > 50 | **实测最大稳定 = 5（达 §9 spec）/ 50 并发 0 错误但 P95 = 65s** | sweep 压测 5/10/20/30/50 |
| KB 召回 Top-5 命中率 | ≥ 80% | 未测 | 离线评测集 |

> **P1 压测结论**：50 并发在 A+B（semaphore=10 + 429 retry）修复后错误率 = 0%，但 P95 总耗时 ~65s。§9 「> 50 并发」与「P95 < 5s」在当前 DashScope 公共 tier 下不可同时满足。详见 `deploy/tests/test_load_sweep.py` 和 `learning_log.md` 模块 10。

### 安全

| 项 | 现状 | 目标 |
|---|---|---|
| 密码 | ✅ bcrypt | — |
| JWT | ✅ httpOnly Cookie | — |
| API Key | ✅ .env（不入库）| — |
| SQL 注入 | ✅ SQLAlchemy 参数化 | — |
| XSS | ✅ 不用 v-html | — |
| **订单越权** | ✅ URL 参数校验 owner | — |
| **Tool user_id 注入** | ✅ service 层强制传 | — |
| 限流 | ⏸ 未实现 | 100 req/min/user |

### 可观测性

| 项 | 现状 | 目标 |
|---|---|---|
| 应用日志 | ✅ | — |
| 审计日志 | ✅ operation_logs | — |
| 链路追踪 | ⏸ | OpenTelemetry（V3）|
| 监控 | ⏸ 仅 /health | Prometheus（V3）|

### 部署

- ✅ Docker Compose（5 服务）
- ⏳ CI/CD
- ⏳ HTTPS

---

## 10. 决策记录

### 已锁定的决策

| # | 议题 | 决定 | 日期 |
|---|---|---|---|
| D1 | 项目代号 | e-commerce-cs-agent | 2026-06-26 |
| D2 | 目标用户 | C 端消费者（demo）| 2026-06-26 |
| D3 | 商业模式 | 开源 demo / 自用 | 2026-06-26 |
| D4 | 商品类目 | 3C 数码（手机 / 耳机 / 手表 / 平板 / 笔记本 / 配件）| 2026-06-26 |
| D5 | 多租户 | 单租户 | 2026-06-26 |
| D6 | 商家后台 | ❌ 不做 | 2026-06-26 |
| D7 | 支付 | ❌ 不做（仅客服）| 2026-06-26 |
| D8 | 模型降级 | 优雅降级（fallback FAQ）| 2026-06-26 |
| D9 | 多语言 | 仅中文 | 2026-06-26 |
| D10 | 数据合规 | 无要求（demo）| 2026-06-26 |

### 讨论记录（按时间倒序）

| 日期 | 议题 | 决定 |
|---|---|---|
| 2026-06-26 | 架构分层 | 3-path：Product RAG / Order Tool / Policy RAG → Synthesizer |
| 2026-06-26 | Agent 路线 | V1.x 不做 Agent，单步 Tool 够用 |
| 2026-06-26 | review_text | V2.x 不入 RAG，独立字段 |
| 2026-06-26 | Rerank | V2.x 跳过，V3 评估 |
| 2026-06-26 | Intent 分类 | 规则优先，LLM 兜底 |
| 2026-06-26 | 多轮对话 | V1.x 弱支持，预留 slot 字段 |
| 2026-06-26 | 商品类目 | 3C 数码 |
| 2026-06-26 | 数据存放 | docs/ecommerce_kb/ 本地保留 |
| 2026-06-26 | 入库方式 | 复用 ingest_text + batch_ingest |

---

## 附录 A：术语表

| 术语 | 含义 |
|---|---|
| RAG | Retrieval-Augmented Generation |
| SSE | Server-Sent Events |
| Top-K | 检索相似度最高的 K 个结果 |
| Chunk | 文档切片 |
| Intent | 用户意图（product / order / refund / policy）|
| Tool | 函数调用层（非 Agent，单步执行）|
| Agent | 自主规划 + 多步推理（V2.x 禁用）|
| Slot | 多轮对话槽位（current_order_id 等）|
| Synthesizer | 多源响应融合器 |

---

## 附录 B：参考资料

- Qdrant 文档：https://qdrant.tech/documentation/
- DashScope API：https://help.aliyun.com/zh/dashscope/
- FastAPI 文档：https://fastapi.tiangolo.com/
- 项目 CLAUDE.md：E:\智能客服\CLAUDE.md