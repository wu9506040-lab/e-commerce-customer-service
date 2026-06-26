# 智能客服 Agent 系统 - 项目开发约束

> 项目级约束，补充本机全局 CLAUDE.md，不覆盖。

## 1. 当前系统架构

| 组件     | 选型                            |
|----------|---------------------------------|
| Backend  | FastAPI                         |
| Vector DB| Qdrant                          |
| Cache    | Redis                           |
| LLM      | Qwen API (DashScope OpenAI 兼容)|
| Deployment | Docker Compose (Docker Desktop + WSL2) |

## 2. 禁止行为

- 禁止引入 Kafka / Milvus / Elasticsearch / 新数据库
- 禁止拆分微服务（保持单体 FastAPI）
- 禁止跨模块写代码（如 RAG 逻辑不进 chat.py）
- 禁止一次性设计"未来系统"（YAGNI）

## 3. 开发原则

- 先最小可运行，再逐步增强
- 所有功能必须模块化（按 app/{api,services,core,rag,clients,schemas,utils} 分层）
- 所有新增能力必须基于现有组件扩展，不重写

## 4. 工作流

**Explore → Plan → Implement → Test**

1. Explore：读相关代码/配置/日志，理解现状
2. Plan：输出方案，等用户确认
3. Implement：最小修改原则，能改一行不改十行
4. Test：curl / log / browser 验证，没验证 = 没完成

## 5. Scope Lock

单次任务只允许修改**一个模块**。

- 单模块：RAG 服务 / Qdrant 客户端 / Chat schema
- 多模块：RAG + Chat 一起改；API + 前端一起改

## 6. 代码结构规范

backend/
└── app/
    ├── api/          # HTTP 接口层（只负责路由）
    ├── services/     # 业务逻辑层（RAG / Chat 编排）
    ├── core/         # 核心能力（LLM / embedding）
    ├── rag/          # RAG 模块（retrieval + pipeline）
    ├── clients/      # 外部服务连接（Qdrant / Redis）
    ├── schemas/      # 请求/响应模型
    └── utils/        # 工具函数

### 分层规则

| 层         | 职责                         | 禁止                       |
|------------|------------------------------|----------------------------|
| api/       | 路由、参数解析、调 services  | 写业务逻辑                 |
| services/  | 业务编排（调 core/rag/clients）| 直接连数据库              |
| core/      | LLM/embedding 等核心能力     | 调外部 HTTP API 路由       |
| rag/       | 检索 + 生成 pipeline         | 写入 chat handler          |
| clients/   | Qdrant/Redis 等外部服务连接  | 写业务逻辑                 |
| schemas/   | Pydantic 模型                | 写逻辑                     |
| utils/     | 纯函数工具                   | 引用其他层                 |

## 7. 项目过程记录（面试与复盘用）

为保证项目可复盘、可讲解、可面试表达，每次关键模块完成后必须记录到：

docs/learning_log.md
（如果不存在则创建）

### 记录要求：

每次完成一个模块（如 RAG / Embedding / Qdrant / API 接入）必须补充：

1. 本模块做了什么（What）
2. 为什么这样设计（Why）
3. 使用了哪些技术（Tech Stack）
4. 输入 → 输出流程（Flow）
5. 遇到的问题 & 解决方案（Problem → Fix）
6. 当前模块在整体系统中的位置（Architecture Role）

---

### 示例结构：

## RAG Pipeline 模块

### What
实现了 query → embedding → Qdrant → LLM 的完整问答链路

### Why
解决传统聊天模型没有知识库的问题，引入外部向量检索增强回答准确性

### Tech
- Qdrant 向量数据库
- OpenAI/Qwen embedding
- FastAPI
- Prompt engineering

### Flow
query → embedding → vector search → top-k → context → LLM → answer

### Problem & Fix
- 问题：中文召回效果差
- 解决：统一 chunk size + overlap + embedding model 升级

### Role
RAG 是整个智能客服系统的“知识增强核心层”
