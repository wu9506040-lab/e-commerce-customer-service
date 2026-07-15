# Frontend (Vue3)

> 占位 — 待开发。

## 技术栈

| 类别 | 选型 |
|---|---|
| 框架 | Vue3 + Vite + TypeScript |
| UI 库 | Element Plus |
| 状态管理 | Pinia |
| HTTP | Axios |
| 路由 | Vue Router 4 |
| 样式 | SCSS |

## 计划目录结构（待定）

```
frontend/
├── src/
│   ├── main.ts             # 入口
│   ├── App.vue
│   ├── api/                # API 客户端
│   ├── views/              # 页面
│   ├── components/         # 组件
│   ├── stores/             # Pinia stores
│   ├── router/             # 路由
│   ├── composables/        # 组合式函数
│   ├── types/              # TypeScript 类型
│   └── utils/              # 工具
├── public/
├── index.html
├── package.json
├── vite.config.ts
├── Dockerfile              # 多阶段：node build → nginx serve
├── nginx.conf
└── .dockerignore
```

## 开发

代码就绪后，本地开发：

```bash
cd frontend
npm install
npm run dev  # 默认 http://localhost:5173
```

容器内（生产模式）：

```bash
docker compose --env-file ../deploy/.env.dev up -d frontend
```

---

## SSE 流式 + 自动断点续传（Sprint P2 / SSE Resume）

### 用户视角

**完全无感**。网络抖动 / 刷新页面 / VPN 切换导致流中断时，前端自动调 `/api/chat/resume` 重连，消息无缝继续显示，**不暴露**任何"续传""AI 正在补救"等提示。

### SSE 协议层

`streamChat(query, sessionId?, opts?)` 使用 `fetch + ReadableStream`（非 EventSource，因为要 POST body）。

每条 SSE event 形如：
```
id: 5
data: {"type":"token","text":"你"}\n\n
```

前端解析 `id:` 行挂到 `event.id`，调用方据此维护 `lastEventId` 状态。

### Resume 调用

`resumeChat(sessionId, streamId, query, lastEventId, opts?)` —— `ChatPage.vue` 在 catch 块自动调用：
- 最多自动 1 次（后端硬限 2 次，剩 1 次留给用户主动重发）
- 失败 fallback：仅显示"消息未送达，请重试"（**不暴露** AI / 流式 / 续传等技术概念）

### 新增事件类型

```typescript
type StreamEvent = ... | {
  type: 'resume_prefix';
  id?: number;
  prefix_text: string;
  from_event_id: number;
  stream_id?: string;
};
```

前端收到 `resume_prefix` 时**替换**而非追加到 `streamingText`（catch 已清空）：

```typescript
case 'resume_prefix':
  streamingText.value = event.prefix_text;
  fullAnswer = event.prefix_text;
  lastEventId = event.from_event_id;
  break;
```

### 为什么不用 EventSource

浏览器原生 EventSource 自动支持 `Last-Event-ID`，但**不支持 POST body**（只有 GET）。本项目用 fetch + ReadableStream 手动解析 `id:` 行，**协议合规**且兼容 POST。

### 为什么"完全无感"是核心 KPI

V3.1 业务架构 §6 明确 AI 客服"聊天窗内降低 AI 感知"是合规边界下的最优解。任何带"续传""AI""补救"字眼的提示都会让用户立刻意识到对面不是人，破坏拟人度。

详细设计见 `docs/learning_log.md §34` + `docs/development/roadmap.md §3.11`。
