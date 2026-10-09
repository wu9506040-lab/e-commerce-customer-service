# 智能客服 Agent 系统

> RAG 检索增强 · 流式问答 · 多轮会话 · 全栈 Docker 化部署

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Vue3](https://img.shields.io/badge/Vue-3.5-4FC08D?logo=vuedotjs)](https://vuejs.org)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python)](https://python.org)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6&logo=typescript)](https://typescriptlang.org)
[![Qdrant](https://img.shields.io/badge/Qdrant-v1.10-DC244C)](https://qdrant.tech)
[![Docker](https://img.shields.io/badge/Docker_Compose-5_services-2496ED?logo=docker)](https://docker.com)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![CI](https://github.com/wu9506040-lab/e-commerce-customer-service/actions/workflows/ci.yml/badge.svg)](https://github.com/wu9506040-lab/e-commerce-customer-service/actions/workflows/ci.yml)
![Tests](https://img.shields.io/badge/pytest-632%20passed-4c1?logo=pytest)
![Coverage](https://img.shields.io/badge/coverage-67%25-yellowgreen)
![Eval](https://img.shields.io/badge/hit%405-0.703%E2%86%920.806%20(%2B10.3pp)-blue?logo=dataflow)

---

## 📊 实测战绩（2026-10-09 本机复跑，全部可一键复现）

| 维度 | 实测值 | 复现命令 |
|------|--------|----------|
| **检索质量** | 330 题分层评测集四级同日连跑（2026-10-09）：dense 0.703 → +BM25/RRF **0.755（+5.2pp）** → +LLM Rerank **0.806（累计 +10.3pp）**；hit@10 0.806→0.882 | `cd deploy && docker compose up -d qdrant redis mysql api`，根目录 `PYTHONPATH=backend python scripts/eval_hitk.py`（逐级加 `--bm25`、`--bm25 --rerank`） |
| **人工介入闭环（M15）** | 转自动落工单（P0 优先队列/认领/回复注入会话/结单），坐席工作台 `/admin/handoff` | 对触发词说"转人工"→ admin 登录看队列 → 回复 → 用户侧会话可见 |
| **测试资产** | **638 passed**（单元 + E2E，含 M15 工单 6 例），覆盖率 **67%** | `cd backend && python -m pytest tests --cov=app` |
| **知识库** | 18 个结构化源文件 → 67 篇文档 → 202 chunks，chunk_id 内容哈希 uuid5 **幂等入库**（重跑/重排零重复） | `PYTHONPATH=backend python scripts/ingest_ecommerce_kb.py` |
| **服务编排** | api / frontend / qdrant / mysql / redis 5 服务 Docker Compose + SSE 流式 + JWT 鉴权 | `deploy/docker-compose.yml` |

> **数据说明**：知识库与评测集为**基于电商售后业务规则构建的合成数据集**（LLM 生成 + 人工抽检校准），
> 评测价值在方法层：二值相关 + hit@K + per-source 分层 + miss 案例落盘，脚本与结果均在仓库内。

## ⚗️ 消融实验与数据诚实声明

- 7 月存档的 0.842 / 0.897 在**当前知识库上不可复现**（KB 内容与配置自 7 月后演进；评测 doc_id 与现库匹配率 109/110，排除 ID 漂移，确认是库变化）——本 README 只采同日同口径连跑值，跨期高点一律不采用
- **更正声明**：本 README 早前版本以 7 月 hybrid(0.842) 对比当日 hybrid_rerank(0.806)，误称"Rerank 负增益 -3.6pp"——跨口径对比本身就是错误读法；同口径下 Rerank 为**正增益（0.755→0.806，+5.1pp）**。此更正正是本项目"数据诚实声明"该防的错误模式，公开留档
- 早期文档中「385 pytest」口径已过时（现为 630+ passed），不再采用——**只写能当场跑出来的数字**
- 公网 ECS 演示已随服务器到期下线；本地 Docker 五服务即为完整运行形态

## 🚧 已知局限（主动交代）

1. 合成 query 与被检索文档同源，存在自匹配偏置；v3 方向为真实对话日志脱敏回流（`operation_log` 全量审计字段已预留）
2. 评测集二值相关 + 单正例设定，hit@K 为保守下界
3. 知识库 202 chunks 小规模，RRF/HNSW 参数结论未在大规模数据验证
4. `scripts/eval_refund_accuracy.py` 存在**双重时间炸弹**：用例硬编码订单号 + seed 订单按日期生成且 7 天退款时效窗口会过期——当前环境跑出 5/22 属数据时序问题非逻辑回归（订单解析/状态机单测全绿）；修法见 backlog：用例运行时从库取号 + seed 滚动日期
5. L3 限流为 INCR+过期 的**固定窗口**计数（边界突发用 ZSET 时间戳实现真滑动，在 backlog）

---

## 📖 项目简介

**智能客服 Agent** 是一个基于 RAG（Retrieval-Augmented Generation）架构的端到端对话系统。
用户提问时，系统先从向量知识库中检索相关资料，再交由大语言模型生成回答，
实现"基于事实、可追溯"的智能问答。

适用于：电商售后、技术支持、企业内部知识库、产品 FAQ 等场景。

**项目特点**：

- 🔍 **完整 RAG 链路** —— query → embedding → Qdrant 检索 → context 组装 → LLM 生成
- ⚡ **SSE 流式输出** —— 大模型逐字返回，前端实时呈现打字效果
- 💬 **多轮对话** —— 基于会话 ID 的上下文管理，支持跨轮指代
- 🔐 **完整鉴权** —— JWT（httpOnly Cookie）+ bcrypt 密码加密 + 角色管理
- 🛡 **可观测性** —— `/health` 三组件探测、`operation_log` 全量审计
- 🐳 **一键部署** —— 5 服务 Docker Compose，开发与生产双套配置
- 📦 **数据闭环** —— admin 端知识入库、来源追溯、chunk 可视化

---

## 📸 效果展示

### 首页 & 商城

| 首页（4 类意图分流） | 商品列表 | 商品详情 |
|---|---|---|
| ![home](frontend/_screenshots/demo.png) | ![shop](frontend/_screenshots/shop.png) | ![detail](frontend/_screenshots/detail.png) |

### 智能客服聊天

| 多轮对话（左历史会话列表 + 右主区） | 上下文：订单 | 上下文：商品 |
|---|---|---|
| ![chat](frontend/_screenshots/chat.png) | ![ctx-order](frontend/_screenshots/ctx-order.png) | ![ctx-product](frontend/_screenshots/ctx-product.png) |

### 完整流程演示（公网 ECS 实测）

8 张顺序截图覆盖登录 → 浏览 → RAG 咨询 → 退款 LangGraph → 个人中心：

[`frontend/_screenshots/walkthrough/demo-01-home.png`](frontend/_screenshots/walkthrough/demo-01-home.png) → [`demo-08-profile.png`](frontend/_screenshots/walkthrough/demo-08-profile.png)

### 订单生命周期流转

`pending` → `paid` → `shipped` → `delivered` → `refunded`（6 张状态截图）：
[`loop-01-product-detail.png`](frontend/_screenshots/loop-01-product-detail.png) → [`loop-06-profile-refunded.png`](frontend/_screenshots/loop-06-profile-refunded.png)

> 截图由 `scripts/verify_demo_public.py` + Playwright 曾在公网 ECS 自动生成（该服务器已到期下线，截图为历史运行存档）。

---

## ✨ 核心特性

| 模块 | 能力 |
|------|------|
| **检索增强（RAG）** | DashScope `text-embedding-v3`（1024 维）→ Qdrant HNSW 索引 → top-k=5 余弦相似度 |
| **流式问答** | SSE（Server-Sent Events）单向推送；前端 `EventSource` 接收 + Markdown 实时渲染 |
| **多轮会话** | MySQL 持久化 + Redis 热路径缓存；cursor 分页拉历史 |
| **知识库管理** | admin 端 `/admin/ingest` 入库（chunk_size + overlap 可配）；`/admin/knowledge/sources` 追溯 |
| **用户系统** | 注册 / 登录 / 改密 / 角色；`/auth/me` 返回用户统计 |
| **会话管理** | 列表 / 详情 / 软删除；按用户隔离 |
| **审计日志** | `operation_log` 表记录 IP / UA / 动作 / 详情 |
| **健康检查** | `/health` 同时探测 mysql / redis / qdrant 状态 |
| **生产部署** | dev / prod 双 compose override；环境变量集中管理；日志与数据卷分离 |

---

## 🛠 技术栈

| 层 | 选型 | 用途 |
|----|------|------|
| 后端框架 | FastAPI 0.115 | REST + SSE，依赖注入，Pydantic 校验 |
| 前端框架 | Vue3 + Vite + TypeScript | 组件化，类型安全 |
| LLM | 通义千问 qwen-max（DashScope OpenAI 兼容）| 流式对话生成 |
| Embedding | DashScope text-embedding-v3 | 1024 维文本向量化 |
| 向量库 | Qdrant v1.10 | 单二进制，REST + gRPC 双协议 |
| 关系库 | MySQL 8.0（utf8mb4）| 用户 / 会话 / 消息 / 知识元数据 / 审计 |
| 缓存 | Redis 7 | 会话热路径，LRU 淘汰 |
| 反向代理 | nginx (alpine) | 前端静态服务 + API 反代 + SSE 流式 |
| 部署 | Docker Desktop + WSL2 | 5 服务一键编排 |

---

## 🏗 系统架构

```
                  ┌─────────────────────────────────────────┐
                  │      Frontend (Vue3 + Vite + TS)        │
                  │   ChatPage / MarkdownView / SSE 接收   │
                  └─────────────────┬───────────────────────┘
                                    │ httpOnly Cookie + SSE
                                    ▼
        ┌───────────────────────────────────────────────────────────┐
        │                 Backend (FastAPI :8000)                   │
        │                                                           │
        │   api/  ─→  services/  ─→  rag/pipeline.py                │
        │   (路由)    (编排)         │                              │
        │              │             │                              │
        │              ▼             ▼                              │
        │       session_service   ┌─────────┐     ┌────────────┐    │
        │       (Redis+MySQL)     │ Qdrant  │     │  Qwen LLM  │    │
        │              │          │ 检索    │     │  流式生成   │    │
        │              ▼          └─────────┘     └────────────┘    │
        │        ┌─────────┐           ▲                            │
        │        │  MySQL  │           │                            │
        │        │ (冷路径) │           │                            │
        │        └─────────┘           │                            │
        └──────────────────────────────┴────────────────────────────┘
                                       │
                              ┌────────┴────────┐
                              │     Redis       │
                              │   (热路径)      │
                              └─────────────────┘
```

**分层原则（严格遵守）**：

| 层 | 职责 | 禁止 |
|----|------|------|
| `api/` | 路由、参数解析、调 services | 写业务逻辑 |
| `services/` | 业务编排（调 core/rag/clients）| 直接连 DB |
| `core/` | LLM / embedding 等核心能力 | 调外部 HTTP API 路由 |
| `rag/` | 检索 + 生成 pipeline | 写入 chat handler |
| `clients/` | Qdrant / Redis / MySQL 连接 | 写业务逻辑 |
| `models/` | ORM 模型 | 写逻辑 |
| `schemas/` | Pydantic 模型 | 写逻辑 |
| `utils/` | 纯函数工具 | 引用其他层 |

---

## 🚀 快速开始

### 前置要求

- Docker Desktop（WSL2 后端）
- 通义千问 API Key（[申请地址](https://dashscope.console.aliyun.com/apiKey)）

### 启动步骤

```bash
# 1. 进入部署目录
cd deploy

# 2. 复制环境变量模板
cp .env.example .env.dev

# 3. 编辑 .env.dev，填入：
#    QWEN_API_KEY=sk-xxxxx
#    JWT_SECRET=<openssl rand -hex 32 生成>

# 4. 启动 5 个服务
docker compose --env-file .env.dev up -d --build

# 5. 验证
curl http://localhost:8000/health
# → {"status":"ok","components":{"mysql":"up","redis":"up","qdrant":"up"}}
```

### 访问入口（本地 Docker 形态）

> 公网 ECS 演示已随服务器到期下线；项目曾完整经历云上部署（安全组 / Nginx 反代 / 健康监控），运行形态以下列本地入口为准。

| 地址 | 说明 | 启动方式 |
|------|------|----------|
| http://localhost:5173 | 前端 Web UI（主入口） | `cd deploy && docker compose up -d` |
| http://localhost:8000/docs | Swagger API 文档（FastAPI 自动生成） | 同上 |
| http://localhost:8000/health | mysql/redis/qdrant 三件套健康检查 | 同上 |
| http://localhost:6333/dashboard | Qdrant 控制台 | 同上 |

### 初始化账号

首次启动**不会**自动创建任何账号，需要手动操作：

```bash
# 1. 注册一个普通用户
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"myuser","password":"mypassword123"}'

# 2. 提升为 admin（直接改 MySQL）
docker exec customer-service-mysql mysql -ucs_user -pcs_pass_2026 customer_service \
  -e "UPDATE users SET role='admin' WHERE username='myuser';"
```

测试账号（已 seed，仅用于演示）：
- `demotest` / `demotest123`：7 个真实订单（pending×2 / paid×1 / shipped×1 / delivered×1 / completed×1 / refunded×1），可演示 LangGraph 退款状态机的 4 路径
- `admin`：通过上述方式手动创建并提权后即可登录 `/api/admin/*` 后台

---

## ⚙️ 配置说明

所有配置集中在 `deploy/.env.example`，关键变量：

| 变量 | 说明 | 必填 |
|------|------|------|
| `QWEN_API_KEY` | 通义千问 API Key | ✅ |
| `JWT_SECRET` | JWT 签名密钥（≥ 32 字符）| ✅ |
| `MYSQL_ROOT_PASSWORD` | MySQL root 密码 | ✅ |
| `MYSQL_PASSWORD` | MySQL 应用账号密码 | ✅ |
| `APP_ENV` | `dev` / `prod` | — |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` | — |

---

## 📡 API 端点

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET | `/health` | 健康检查（mysql/redis/qdrant）| ❌ |
| POST | `/auth/register` | 注册 | ❌ |
| POST | `/auth/login` | 登录（form → Set-Cookie JWT）| ❌ |
| GET | `/auth/me` | 当前用户 + 统计 | ✅ |
| POST | `/chat` | RAG 多轮问答（SSE 流式）| 可选 |
| GET | `/conversations` | 当前用户会话列表 | ✅ |
| GET | `/conversations/{sid}/messages` | 会话消息（cursor 分页）| ✅ |
| DELETE | `/conversations/{sid}` | 软删会话 | ✅ |
| POST | `/admin/ingest` | 知识入库 | admin |
| GET | `/admin/knowledge/sources` | 知识来源列表 | admin |

---

## 📚 文档

| 文档 | 内容 |
|------|------|
| [`docs/learning_log.md`](docs/learning_log.md) | 项目演进日志（1472 行，14 个模块，每个含 What / Why / Tech / Flow / Problem→Fix / Role）|
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | 运维指南（端口、数据卷、故障排查、生产部署）|
| [`docs/HEALTHCHECK.md`](docs/HEALTHCHECK.md) | healthcheck.io 接入指南（5 min 接入 + 告警演练）|
| [`docs/test_coverage.md`](docs/test_coverage.md) | 测试覆盖矩阵（115 单元 + ~100 E2E，10 个测试文件）|

---

## 🔧 设计决策（关键技术选择的理由）

### 1. 为什么用 SSE 而不是 WebSocket？

LLM 输出是**单向服务器推送**，SSE 直接复用 HTTP（鉴权、反代都简单）；
WebSocket 双向协议反而过度设计。

### 2. 为什么 MySQL + Redis 双写？

- **MySQL**：持久化、消息历史可追溯、支持 cursor 分页
- **Redis**：会话热路径，避免每次请求都打 DB
- **写穿透策略**：MySQL 失败仅 warning，不影响 SSE done 事件（best-effort）

### 3. 为什么选 Qdrant 而不是 Milvus / ES？

- **单二进制部署**：Docker Desktop 友好
- **REST + gRPC 双协议**：调试方便
- **HNSW 默认索引**：ANN 检索速度足够
- **payload 过滤 + 向量检索**混合能力强

### 4. Qdrant 为什么是 1024 维？

阿里 DashScope `text-embedding-v3` 默认输出 1024 维。
Qdrant collection 的 `vector_size` 必须**严格匹配**，否则报 `Wrong dimensions` 错误。
两边常量在模块顶部加注释强制同步。

### 5. 流式滚动为什么需要节流？

每收一个 token 就 `scrollTop` 会卡顿（DOM 操作阻塞主线程）。
用 `requestAnimationFrame` 把 50ms 内的多次 scroll 合批，性能提升 ~10x。

### 6. 为什么不用 LangChain？

- **过度抽象**：业务逻辑藏在 chain 里，调 bug 翻三层
- **依赖重**：本项目用 OpenAI SDK 直调，3 行代码解决
- **可读性**：直调更便于理解底层 LLM 调用机制

---

## 🗂 目录结构

```
E:\智能客服\
├── backend/                  # FastAPI 后端
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
│   ├── docker-compose.yml    # 5 服务开发环境
│   ├── docker-compose.prod.yml
│   └── .env.example
├── docs/                     # 项目文档
│   ├── learning_log.md       # 演进日志（1472 行）
│   └── OPERATIONS.md         # 运维指南
├── LICENSE                   # MIT
└── README.md                 # ← 你正在看
```

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。

开发前请阅读：
- `docs/learning_log.md` —— 了解项目演进历程和设计权衡
- 各模块顶部的中文 docstring —— 说明模块职责和禁止事项

---

## 📄 License

[MIT](LICENSE)