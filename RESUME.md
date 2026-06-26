# 智能客服 Agent 系统

> RAG · 流式问答 · 多轮对话 · 全栈 Docker 化 · 面向生产环境的 LLM 应用

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Vue3](https://img.shields.io/badge/Vue-3.5-4FC08D?logo=vuedotjs)](https://vuejs.org)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python)](https://python.org)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6?logo=typescript)](https://typescriptlang.org)
[![Qdrant](https://img.shields.io/badge/Qdrant-v1.10-DC244C)](https://qdrant.tech)
[![Docker](https://img.shields.io/badge/Docker_Compose-5_services-2496ED?logo=docker)](https://docker.com)

---

## 🎯 一句话定位

**一个可独立部署、可演示、面向生产的 RAG 智能客服后端**——
前端 Vue3 流式 UI + 后端 FastAPI 检索增强 + Qdrant 向量库 + Qwen LLM，
五服务 Docker Compose 一键拉起，含鉴权、审计、会话管理、知识入库的完整闭环。

---

## ✨ 核心亮点（面试官 30 秒看完）

| # | 能力 | 体现 |
|---|---|---|
| 1 | **完整 RAG 链路** | query → embedding → Qdrant top-k → context 组装 → LLM 流式生成 |
| 2 | **SSE 流式问答** | 边收 token 边 yield，打字机光标 + Markdown 实时渲染 + 代码块一键复制 |
| 3 | **多轮会话 + 写穿透** | MySQL 持久化 + Redis 热路径缓存，cursor 分页拉历史 |
| 4 | **完整鉴权体系** | JWT（httpOnly Cookie）+ bcrypt 加密 + 角色（admin/user）+ `/me` 用户统计 |
| 5 | **生产级部署** | dev/prod 双 compose override；五服务编排；数据卷全在 E 盘 |
| 6 | **可观测性** | `/health` 聚合 mysql/redis/qdrant 状态；`operation_log` 全量审计 |
| 7 | **知识运营闭环** | admin `/admin/ingest` 入库 → `/admin/knowledge/sources` 追溯 |

---

## 🏗 系统架构

```
                    ┌─────────────────────────────────────┐
                    │     Frontend (Vue3 + Vite + TS)     │
                    │  ChatPage / MarkdownView / SSE 接收 │
                    └─────────────────┬───────────────────┘
                                      │ httpOnly Cookie + SSE
                                      ▼
        ┌─────────────────────────────────────────────────────────┐
        │              Backend (FastAPI :8000)                    │
        │                                                         │
        │   api/  ─→  services/  ─→  rag/pipeline.py              │
        │   (路由)    (编排)         │                             │
        │              │             │                             │
        │              ▼             ▼                             │
        │       session_service   ┌─────────┐    ┌────────────┐   │
        │       (Redis+MySQL)     │ Qdrant  │    │  Qwen LLM  │   │
        │              │          │ 检索    │    │  流式生成   │   │
        │              ▼          └─────────┘    └────────────┘   │
        │        ┌─────────┐            ▲                           │
        │        │  MySQL  │            │                           │
        │        │ (冷路径)│            │                           │
        │        └─────────┘            │                           │
        └──────────────────────────────┴───────────────────────────┘
                                       │
                              ┌────────┴────────┐
                              │     Redis       │
                              │   (热路径)      │
                              └─────────────────┘
```

**分层原则（严格，违反即重构）**：

| 层 | 职责 | 禁止 |
|---|---|---|
| `api/` | 路由、参数解析、调 services | 写业务逻辑 |
| `services/` | 业务编排（调 core/rag/clients）| 直接连 DB |
| `core/` | LLM / embedding 核心能力 | 调 HTTP 路由 |
| `rag/` | 检索 + 生成 pipeline | 写入 chat handler |
| `clients/` | Qdrant / Redis 连接 | 写业务逻辑 |
| `models/` | ORM 模型 | 写逻辑 |
| `schemas/` | Pydantic 模型 | 写逻辑 |
| `utils/` | 纯函数工具 | 引用其他层 |

---

## 🚀 30 秒启动

```bash
cd deploy
cp .env.example .env.dev       # 填 QWEN_API_KEY 和 JWT_SECRET
docker compose --env-file .env.dev up -d --build

curl http://localhost:8000/health
# → {"status":"ok","components":{"mysql":"up","redis":"up","qdrant":"up"}}
```

打开 http://localhost:8000/docs 看 Swagger UI。

---

## 🧠 技术深度（面试可讲的"为什么"）

### 1. 为什么用 SSE 而不是 WebSocket？

| 维度 | SSE | WebSocket |
|---|---|---|
| 协议 | HTTP（单向） | 独立协议（双向）|
| 鉴权 | 直接复用 httpOnly Cookie | 需手动 header 携带 |
| 反代 | nginx 配 `proxy_buffering off` 即可 | 需 `Upgrade` 头 |
| 复杂度 | 低 | 高 |
| 适用 | LLM 流式输出（单向推送） | 双向实时通信 |

LLM 输出是**单向服务器推送**，用 SSE 复杂度低一半。

### 2. 为什么 MySQL + Redis 双写？

- **MySQL**：持久化、消息历史可追溯、支持 cursor 分页
- **Redis**：会话热路径，避免每次请求都打 DB；LRU 淘汰保护内存
- **写穿透策略**：MySQL 失败仅 warning，不影响 SSE done 事件（best-effort）

### 3. 为什么 Qdrant 而不是 Milvus / ES？

- **单二进制部署**：Docker Desktop 友好，5 分钟跑起来
- **REST + gRPC 双协议**：调试方便
- **HNSW 默认索引**：ANN 检索速度足够
- **过滤能力强**：payload 过滤 + 向量检索混合

### 4. 为什么拆出 `with_safe_session` / `chat_history` 三个文件？

- `chat_history.py` 单文件 800+ 行时，bug 修复要翻全栈
- 拆成 `load / persist / schema` 三个文件，单文件 ≤ 300 行
- 单测可以独立 mock 各模块

### 5. Qdrant 维度为什么是 1024？

阿里 DashScope `text-embedding-v3` 默认输出 1024 维。Qdrant collection 的 `vector_size` 必须**严格匹配**，否则 `Wrong dimensions` 报错。把两边常量提到模块顶部加注释同步。

### 6. 流式滚动为什么需要节流？

每收一个 token 就 `scrollTop` 会卡顿（DOM 操作阻塞主线程）。
用 `requestAnimationFrame` 把 50ms 内的多次 scroll 合批，性能提升 ~10x。

### 7. 为什么没用 LangChain？

- **过度抽象**：业务逻辑藏在 chain 里，调 bug 翻三层
- **依赖重**：本项目用 OpenAI SDK 直调，3 行代码解决
- **学习价值**：直调更能体现 LLM 调用、prompt 工程的"基本功"

---

## 📂 项目结构

```
E:\智能客服\
├── backend/                  # FastAPI 后端（46 个 .py 文件）
│   ├── app/
│   │   ├── api/              # HTTP 路由（auth / chat / conversations / admin）
│   │   ├── services/         # 业务编排（session / auth / rag / audit）
│   │   ├── core/             # 核心能力（config / security / embedding / qwen）
│   │   ├── rag/              # 检索 pipeline + ingest
│   │   ├── clients/          # Qdrant / Redis / MySQL 连接
│   │   ├── models/           # ORM 模型（5 张表）
│   │   ├── schemas/          # Pydantic 模型
│   │   └── main.py           # FastAPI 入口
│   ├── requirements.txt      # 依赖锁版本
│   └── Dockerfile
├── frontend/                 # Vue3 前端
│   └── src/components/       # 6 个组件（ChatPage / MessageList / MarkdownView…）
├── deploy/                   # Docker Compose 编排
│   ├── docker-compose.yml    # 5 服务
│   ├── docker-compose.prod.yml
│   └── .env.example
├── docs/learning_log.md      # 模块复盘（1472 行，What/Why/Tech/Flow/Problem→Fix）
├── README.md                 # 运维向启动 / 故障排查
└── RESUME.md                 # ← 你正在看
```

---

## 📊 关键指标（自测）

| 指标 | 值 |
|---|---|
| 后端代码量 | 46 个 .py，约 3500 行 |
| 前端代码量 | 6 个 .vue 组件 + api.ts，约 1500 行 |
| Docker 服务数 | 5（api / frontend / qdrant / redis / mysql）|
| MySQL 表数 | 5（user / conversation / message / knowledge_document / operation_log）|
| API 端点数 | 9（auth × 3 + chat × 1 + conversations × 3 + admin × 2 + health）|
| 开发周期 | V1.0 ~ V1.2 迭代 14 个业务模块 |

---

## 🔌 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（mysql/redis/qdrant 三组件）|
| POST | `/auth/register` | 注册 |
| POST | `/auth/login` | 登录（form → Set-Cookie JWT）|
| GET | `/auth/me` | 当前用户 + stats |
| POST | `/chat` | RAG 多轮问答（SSE 流式）|
| GET | `/conversations` | 会话列表 |
| GET | `/conversations/{sid}/messages` | 会话消息（cursor 分页）|
| DELETE | `/conversations/{sid}` | 软删会话 |
| POST | `/admin/ingest` | 知识入库（admin）|
| GET | `/admin/knowledge/sources` | 知识来源列表（admin）|

Swagger: `http://localhost:8000/docs`

---

## 🎤 面试高频问答（直接讲）

### Q1：RAG 的整体链路是什么？
query → embedding（DashScope text-embedding-v3，1024 维）→ Qdrant 向量检索 top-k=5
→ context 用 `[1]/[2]/[3]` 编号拼接 → 注入 system prompt 约束 LLM 仅基于参考资料回答
→ Qwen 流式生成 → SSE 边收 token 边 yield → 客户端打字机渲染。

### Q2：SSE 和 WebSocket 怎么选？
LLM 输出是单向服务器推送，SSE 直接复用 HTTP（鉴权、反代都简单），WebSocket 双向协议反而过度设计。

### Q3：MySQL 和 Redis 双写怎么保证一致性？
不保证，写穿透策略：MySQL 失败仅 warning，不阻塞 SSE done 事件（best-effort）。
对话历史允许偶发丢失（用户重发即可），不引入分布式事务复杂度。

### Q4：Qdrant 维度不一致怎么办？
启动时 `ensure_collection` 检查 `vector_size == 1024`，不一致抛 `Wrong dimensions`。
把两边常量都提到模块顶部 + 注释强制同步。

### Q5：Qwen 流式断流怎么办？
客户端用 `EventSource` + 自动重连；
服务端 nginx 配 `proxy_buffering off` + `X-Accel-Buffering: no`。

### Q6：JWT 怎么防 XSS / CSRF？
- XSS：httpOnly Cookie，JS 读不到 token
- CSRF：SameSite=Lax + 双重提交 token（接口要求 header 带 token）
- bcrypt 加密密码存储

### Q7：怎么定位 LLM 召回不准？
Qdrant payload 里存 `source`，前端点击 chunk 可跳转到原始文档；
查 top-k 分数（cosine similarity），阈值 < 0.6 视为召回失败。

### Q8：前后端怎么分层？
api 只调 services，services 编排 core/rag/clients，core 调 LLM/embedding，
clients 连 Qdrant/Redis/MySQL。models 只做 ORM，schemas 只做校验。
单文件 ≤ 300 行，违反即重构。

### Q9：为什么不用 LangChain？
过度抽象，业务逻辑藏在 chain 里难调试；
本项目 OpenAI SDK 直调 3 行解决，学习价值更高（面试更出彩）。

### Q10：如果让你上 Agent 怎么做？
- Tools：注册 query_kb / query_db / create_ticket，OpenAI function calling 调度
- Memory：短期 Redis 上下文 + 长期 MySQL 用户偏好
- Plan：ReAct 框架，思考 → 行动 → 观察 循环
- 限流：每用户每分钟 10 次，避免 token 暴增

---

## 📚 学习日志

`docs/learning_log.md` 按模块记录 **What / Why / Tech / Flow / Problem→Fix / Architecture Role** 六段，
1472 行。完整覆盖 14 个业务模块，是项目复盘的核心资产。

---

## 📄 License

[MIT](LICENSE)