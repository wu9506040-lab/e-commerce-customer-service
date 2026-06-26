# 智能客服 Agent 系统

> RAG + 流式问答 + 多轮会话，全栈 Docker 化，面试可讲。

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 前端 | Vue3 + Vite + TypeScript | SSE 流式接收 + Markdown 渲染 |
| 后端 | FastAPI + Python 3.11 | RAG 检索增强 + 多轮对话 |
| LLM | 阿里云通义千问（DashScope OpenAI 兼容）| `qwen-max` 默认 |
| 向量库 | Qdrant v1.10 | 单二进制，REST + gRPC |
| 缓存 | Redis 7 | 会话热路径 + LRU 淘汰 |
| 数据库 | MySQL 8.0（utf8mb4）| 用户 / 会话 / 审计 / 知识元数据 |
| 部署 | Docker Desktop + WSL2 | 一键起 |

## 目录结构

```
E:\智能客服\
├── deploy\                   # Docker 部署
│   ├── docker-compose.yml    # 服务编排（5 服务）
│   ├── docker-compose.prod.yml  # 生产 override
│   ├── .env.example          # 环境变量模板（与 config.py 对齐）
│   ├── .env.dev              # 开发环境值（不提交）
│   ├── frontend\             # nginx 占位 + 配置
│   └── mysql\init\           # MySQL 初始化 SQL
├── backend\                  # FastAPI 后端
│   ├── app\                  # api / services / rag / clients / core / schemas
│   ├── requirements.txt      # Python 依赖（锁版本）
│   └── Dockerfile
├── frontend\                 # Vue3 前端
│   ├── src\components\       # ChatPage / MessageList / MarkdownView …
│   ├── package.json          # Vue3 + marked
│   └── vite.config.ts
├── docs\                     # 学习日志 + 架构笔记
└── README.md                 # 本文件
```

## 快速启动（开发环境）

### 1. 准备环境

```powershell
# WSL2 + Docker Desktop + 镜像加速（registry-mirrors）已配
docker run hello-world
```

### 2. 配置环境变量

```powershell
cd E:\智能客服\deploy
cp .env.example .env.dev
# 编辑 .env.dev，必填：
#   QWEN_API_KEY=sk-xxxxx  (从 https://dashscope.console.aliyun.com/apiKey 获取)
#   JWT_SECRET=（openssl rand -hex 32 生成 64 字符）
```

### 3. 启动服务

```powershell
docker compose --env-file .env.dev up -d --build
```

### 4. 验证

```powershell
# API 健康检查（含 mysql/redis/qdrant 状态）
curl http://localhost:8000/health

# 浏览器
#   API Swagger:  http://localhost:8000/docs
#   前端:         http://localhost:5173   （如启用 frontend）
#   Qdrant UI:    http://localhost:6333/dashboard

# 初始化账号（admin / admin123，首次登录后改密）
```

## 生产部署

```powershell
# 1. 准备 .env.prod（参考 .env.example，填真实值）
cp .env.example .env.prod

# 2. 关键差异
#    - JWT_SECRET 必须 openssl rand -hex 32 生成
#    - COOKIE_SECURE=true（要求 HTTPS，前置 nginx/caddy 终止）
#    - APP_ENV=prod / LOG_LEVEL=WARNING
#    - 暴露 FRONTEND_PORT（默认 5173）

# 3. 启动（用 -f 合并，dev compose 是配置源）
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
               --env-file .env.prod up -d --build

# 4. 升级
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
               --env-file .env.prod build api
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
               --env-file .env.prod up -d api
```

## 端口清单

| 服务 | 端口 | 用途 |
|---|---|---|
| frontend | 5173 | Web UI（nginx 80 映射）|
| api | 8000 | FastAPI REST + Swagger `/docs` |
| qdrant | 6333 / 6334 | REST API / gRPC |
| redis | 6379 | 缓存 |
| mysql | 3307 → 3306 | 数据库（宿主 3307 避开本机） |

## 数据卷位置

所有数据持久化到 **E 盘**（避免 C 盘紧张）：

| 卷 | 路径 |
|---|---|
| Qdrant | `E:\DockerData\volumes\qdrant` |
| Redis | `E:\DockerData\volumes\redis` |
| MySQL | `E:\DockerData\volumes\mysql` |
| 上传文件 | `E:\DockerData\volumes\uploads` |
| 应用日志 | `E:\DockerData\volumes\logs` |

## 常用命令

```powershell
# 启动
docker compose --env-file .env.dev up -d

# 实时日志
docker compose --env-file .env.dev logs -f api

# 进入容器调试
docker compose --env-file .env.dev exec api bash

# 停止（保留数据）
docker compose --env-file .env.dev down

# 重置（⚠️ 删数据）
docker compose --env-file .env.dev down -v

# 改代码后重建 API
docker compose --env-file .env.dev build api
docker compose --env-file .env.dev up -d api
```

## 故障排查

| 症状 | 排查 |
|---|---|
| `/chat` 返回 500 "QWEN_API_KEY 未配置" | `.env.dev` 中 `QWEN_API_KEY` 未填或为占位符；重启 `docker compose up -d api` |
| `/health` 提示 `redis: down` | `docker logs customer-service-redis` 看启动错；常见是 WSL2 重启后 DNS 缓存失效，等 5s 重试 |
| `curl localhost:8000` 连不上 | `docker ps` 看 api 容器是否 Running；`docker logs customer-service-api` 看启动日志 |
| 端口 3306/6379/6333 占用 | MySQL 宿主端口已改 3307；Redis/Qdrant 端口如冲突，编辑 `.env.dev` 加 `ports:` 映射或停本地服务 |
| MySQL 容器 OOM 反复重启 | 数据量大时调高 `deploy.resources.limits.memory`（compose.yml 默认 512M）|
| 前端 SSE 流式断流 | Nginx 反代需加 `proxy_buffering off` + `proxy_http_version 1.1`；浏览器 devtools 看 network 是否 text/event-stream |
| `npm run build` TS 报错 | `cd frontend && rm -rf node_modules package-lock.json && npm install` |
| `python-bcrypt` 安装失败（Windows 本地） | Docker 容器内无此问题；本地调试用 `pip install bcrypt` 而非 `pip install bcrypt-binary` |

## 当前状态

- ✅ V1.0：14 业务模块 + RAG + MySQL schema + Auth
- ✅ V1.1：with_safe_session / Config 集中化 / chat_history 拆 3 文件 / Markdown + 流式光标 + 空态
- ✅ V1.2：/health 组件状态 / /me 用户统计 / 流式滚动节流 / 代码块复制 / skeleton / prod compose / .env 补全
- ⏳ V2.0：Agent（用户当前禁用）

## API 端点速查

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含 mysql/redis/qdrant 状态）|
| POST | `/auth/register` | 注册 |
| POST | `/auth/login` | 登录（form → Set-Cookie JWT）|
| GET | `/auth/me` | 当前用户 + stats |
| POST | `/chat` | RAG 多轮问答（SSE 流式）|
| GET | `/conversations` | 当前用户会话列表 |
| GET | `/conversations/{sid}/messages` | 会话消息历史（cursor 分页）|
| DELETE | `/conversations/{sid}` | 软删会话 |
| POST | `/admin/ingest` | 知识入库（admin）|
| GET | `/admin/knowledge/sources` | 知识来源列表（admin）|

## 学习日志

`docs\learning_log.md` — 记录每个模块的 What / Why / Tech / Flow / Problem→Fix / Architecture Role（面试复盘用）。
