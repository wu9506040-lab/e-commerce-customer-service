# Backend (FastAPI)

> 占位 — 待开发。

## 技术栈

| 类别 | 选型 |
|---|---|
| Web 框架 | FastAPI |
| Python | 3.11 |
| Agent | LangGraph |
| ORM | SQLAlchemy 2.0 + PyMySQL |
| 向量库客户端 | qdrant-client |
| 缓存客户端 | redis-py |
| LLM SDK | dashscope（OpenAI 兼容模式）|
| 配置 | pydantic-settings |
| 日志 | loguru |

## 计划目录结构（待定）

```
backend/
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── api/                 # 路由
│   ├── core/                # 配置、日志
│   ├── services/            # 业务逻辑（RAG、Agent）
│   ├── models/              # SQLAlchemy 模型
│   ├── schemas/             # Pydantic schema
│   └── clients/             # 外部服务客户端（Qdrant/Redis/Qwen）
├── tests/
├── requirements.txt
├── Dockerfile
└── .dockerignore
```

## 开发

代码就绪后，本地开发：

```bash
# 容器外（连 docker 内的 MySQL/Redis/Qdrant）
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

容器内（生产模式）：

```bash
docker compose --env-file ../deploy/.env.dev up -d api
```
