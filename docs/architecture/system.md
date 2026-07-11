# 系统架构 · System Architecture（V2.1）

> 项目级系统说明，**业务基线** 见 `business.md`，**架构铁律** 见 `CLAUDE.md §9`。
>
> 维护者：Tech Lead
> 最近更新：2026-07-11

---

## 0. 文档目的

| 读者       | 应在本文档获取的信息                            |
|------------|-------------------------------------------------|
| 新成员     | 系统由哪些模块组成、彼此关系、技术选型          |
| AI 编程工具 | 改动前必读的"系统形状"事实，避免破坏整体结构   |
| 面试官     | 系统拓扑、技术决策、模块化程度                  |
| 运营       | 依赖服务清单、环境变量、部署边界                |

---

## 1. 系统拓扑

### 1.1 一句话总览

电商智能客服 Agent：FastAPI 单体 + Qdrant 向量库 + Redis 缓存 + MySQL 业务库 + Qwen (DashScope) 大模型，前端 Vue3 + TypeScript，Docker Compose 容器化部署。

### 1.2 组件清单

| 类别       | 组件                          | 角色                                          |
|------------|-------------------------------|-----------------------------------------------|
| Backend    | FastAPI (Python ≥ 3.11)       | API 服务、业务编排、RAG pipeline              |
| Frontend   | Vue3 + TypeScript + Vite      | 客服工作台 / 演示前端                         |
| Vector DB  | Qdrant                        | 知识库向量检索                                |
| Cache      | Redis                         | 会话缓存 / 限流 / 计数器                      |
| RDBMS      | MySQL 8.0                     | 业务数据（订单 / 会话 / 知识条目）            |
| LLM        | Qwen API (DashScope OpenAI 兼容) | 主模型 / Embedding / Rerank                 |
| Deployment | Docker Compose                | Docker Desktop (WSL2) / 阿里云 ECS           |

### 1.3 部署形态

| 环境       | 形态                                               |
|------------|----------------------------------------------------|
| 本地开发   | Docker Desktop (WSL2) · `deploy/docker-compose.dev.yml` |
| 公网演示   | 阿里云 ECS · `deploy/docker-compose.prod.yml`     |
| 数据持久化 | Docker volume + `.env.dev` / `.env.prod`           |

> **禁止引入 Kafka / Milvus / Elasticsearch / 新数据库。**
> **禁止拆分微服务（保持单体 FastAPI）。**

---

## 2. 分层架构

```
┌────────────────────────────────────────────────────────┐
│ API 层             FastAPI Router · Pydantic Schema   │
├────────────────────────────────────────────────────────┤
│ Services 层        业务编排（chat / order / rag orchestration）│
├────────────────────────────────────────────────────────┤
│ RAG 层             文档处理 / 向量化 / 召回 pipeline  │
├────────────────────────────────────────────────────────┤
│ Core 能力层        LLMProvider / EmbeddingProvider / RerankProvider │
├────────────────────────────────────────────────────────┤
│ Clients 层         Qdrant / Redis / MySQL 客户端      │
├────────────────────────────────────────────────────────┤
│ Schemas / Utils    Pydantic Schema / 纯函数工具        │
└────────────────────────────────────────────────────────┘
```

### 2.1 分层约束

| 层              | 职责                                       | 禁止                                       |
|-----------------|--------------------------------------------|--------------------------------------------|
| api/            | 路由、参数解析、调 services                | 写业务逻辑                                 |
| services/       | 业务编排（调 core/rag/clients）            | 直接连数据库                              |
| core/           | LLM / Embedding / Rerank 等能力           | 调 HTTP API 路由（业务编排归 services）     |
| rag/            | 检索 + 生成 pipeline                       | 写入 chat handler                          |
| clients/        | Qdrant / Redis / MySQL 客户端             | 写业务逻辑                                 |
| schemas/        | Pydantic Schema                             | 写逻辑                                     |
| utils/          | 纯函数工具                                  | 引用其他层                                 |

### 2.2 接口就近原则（Protocol 放置）

| 形态                               | 位置                                  |
|------------------------------------|---------------------------------------|
| 一个模块的抽象接口                 | `app/services/<module>/protocols.py`  |
| Provider 类抽象                    | `app/core/<capability>/protocols.py`  |
| 客户端抽象（Qdrant/MySQL）         | `app/clients/<vendor>/protocols.py`   |
| 跨模块"系统级"抽象（如 EventBus）  | `app/core/contracts/`                 |

---

## 3. 运行时架构

### 3.1 典型请求链路（聊天接口）

```
1. HTTP POST /api/chat
       │
       ↓
2. api/chat.py：路由 + 参数校验 + tenant 提取
       │
       ↓
3. services/chat/chat_service.py：业务编排
       ├── InputGuard 鉴黄 / 反注入 / 反 PII（不调 LLM）
       ├── Conversation Buffer 聚合短时间消息
       ├── RAG 检索（rag/pipeline.py → core/providers/embedding + clients/qdrant）
       ├── Emotion 分析（如启用）
       ├── Agent 决策（core/providers/llm）
       ├── Order 工具调用（如需要 → services/order）
       └── 输出组装
       │
       ↓
4. Response → JSON 输出
```

### 3.2 数据流

```
    ┌─────────────┐
    │   用户会话  │ ←── Redis（短期 Buffer）
    └─────┬───────┘
          │
          ↓
   ┌──────────────┐        ┌──────────────────┐
   │  MySQL 业务   │ ←──── │  Order Service  │
   │  - 订单       │        └──────────────────┘
   │  - 会话历史   │
   └──────────────┘
          ↑
          │
   ┌──────────────┐        ┌──────────────────┐
   │   Qdrant     │ ←──── │  RAG Pipeline    │
   │  - 知识条目   │        └──────────────────┘
   └──────────────┘
          ↑
          │
   ┌──────────────┐
   │ Qwen API     │ ←── LLM / Embedding / Rerank
   └──────────────┘
```

---

## 4. 关键能力矩阵

| 能力         | 当前实现                       | 抽象接口            | 扩展点                              |
|--------------|--------------------------------|---------------------|-------------------------------------|
| 大模型       | Qwen (DashScope OpenAI 兼容)  | `LLMProvider`       | GPT / Claude / 本地 vLLM           |
| 向量化       | DashScope Embedding            | `EmbeddingProvider` | BGE / OpenAI Embedding             |
| 重排序       | DashScope Rerank               | `RerankProvider`    | Cohere Rerank                       |
| 向量数据库   | Qdrant                         | `VectorStore`       | Milvus / Weaviate                  |
| 业务数据库   | MySQL 8.0                      | `Database`          | PostgreSQL / TiDB                  |
| 缓存         | Redis                          | `Cache`             | Memcached                            |
| 对象存储     | （暂未启用）                   | `ObjectStorage`     | 阿里云 OSS / MinIO                  |
| 事件总线     | 进程内 EventBus               | `EventBus`          | 禁止 Kafka / MQ                     |
| 监控         | healthcheck.io                 | n/a                 | Prometheus（按需）                  |

---

## 5. 配置与密钥

### 5.1 配置文件分层

| 文件                     | 用途                                  |
|--------------------------|---------------------------------------|
| `deploy/.env.example`    | 全量变量清单（不进 Git）              |
| `deploy/.env.dev`        | 本地开发（gitignore）                 |
| `deploy/.env.prod`       | 生产（gitignore / ECS 安全保存）      |
| `config/prompts/`        | Prompt 模板（YAML）                   |
| `config/business_rules/` | 业务规则（YAML，如情绪阈值）          |

### 5.2 密钥管理

- **永远从环境变量读取** API Key / Secret / Token
- **禁止硬编码** API Key
- **禁止把 .env 文件提交到 Git**

---

## 6. 可观测性

| 维度       | 实现                                       |
|------------|--------------------------------------------|
| 健康检查   | `/healthz` + healthcheck.io                |
| 日志       | 结构化 JSON（`request_id` / `tenant_id` / `user_id` / `conversation_id` / `latency` / `tokens`） |
| 指标       | 当前阶段轻量；后续接 Prometheus             |
| 追踪       | 当前阶段单服务内 trace_id                   |

### 6.1 必记录字段（与 CLAUDE.md §9.5.2 对齐）

| 字段                  | 用途                |
|-----------------------|---------------------|
| `request_id`          | 单次请求追踪        |
| `tenant_id`           | 多租户隔离（默认 `"default"`） |
| `user_id`             | 用户级分析          |
| `conversation_id`     | 会话级分析          |
| 模型调用（输入/输出） | 性能 + 成本分析     |
| Token 消耗            | 成本统计            |
| Tool 调用（参数/结果）| 业务流追踪          |
| 响应时间              | 性能监控            |
| 异常信息（含堆栈）    | 问题定位            |

---

## 7. 非功能需求

| 维度       | 目标                                       |
|------------|--------------------------------------------|
| 性能       | 单轮问答 < 3s（不包含 RAG 检索）           |
| 可用性     | 主要接口 P99 错误率 < 1%                    |
| 安全       | BCrypt / API Key / Tenant 隔离 / 5 防      |
| 可维护     | 单模块可替换 / 单模块可独立测试            |
| 可扩展     | 多平台 / 多 Agent / 多租户（接口预留）    |

---

## 8. 参考

- 业务基线：`docs/architecture/business.md`
- 工程纪律：仓库根 `CLAUDE.md`
- AI 开发规则：`docs/governance/ai_development_rules.md`
- 演进路线：`docs/development/roadmap.md`
