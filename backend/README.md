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

---

## SSE 流式接口（Sprint P2 / SSE Resume）

### POST /api/chat（SSE 流式 + 自动断点续传）

RAG 多轮问答（SSE 流式）。返回 `text/event-stream`，事件类型：
- `meta`：意图/实体/检索 contexts（含 `stream_id`，前端用于续传）
- `token`：流式文本片段
- `heartbeat`：30s 心跳
- `done`：流结束（含最终 `session_id`）
- `error`：生成错误
- `closed`：服务端优雅关闭

每条 event 自带 `id: {seq}\n` 行（SSE 标准 `Last-Event-ID` 协议）。

### POST /api/chat/resume（SSE 流式中断续传）

客户端在 `/chat` 流中断（未收到 `done`）后调用。后端从 Redis checkpoint 读取已流 prefix 一次性重发，**不调 LLM**（MVP 边界）。

**请求**：
```json
{
  "session_id": "uuid...",
  "stream_id": "abc123def456",
  "query": "退款流程是怎样的？",
  "last_event_id": 5,
  "sku": null,
  "order_no": null
}
```

**响应**（SSE）：
```
id: 1
data: {"type":"resume_prefix","prefix_text":"退款流程是先...","from_event_id":5,"stream_id":"..."}\n\n

id: 2
data: {"type":"done","session_id":"..."}\n\n

id: 3
data: {"type":"closed"}\n\n
```

**错误码**：
| 状态码 | 含义 |
|--------|------|
| 410 Gone | checkpoint 不存在 / TTL 过期 / query 不匹配 / resume 次数超限（≥2） |

**关键约束**：
- Checkpoint TTL：**600s**（覆盖典型网络抖动 + 重连时间）
- Resume 限流：同 `(session_id, stream_id)` 最多 2 次
- 不调 LLM：仅重发 prefix，不续写不补全

详细设计见 `docs/learning_log.md §34` + `docs/development/roadmap.md §3.11`。
