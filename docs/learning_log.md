# 智能客服 Agent 系统 - 学习日志

> 按项目演进日志规范记录。模块顺序按开发时间线，每个模块包含 What / Why / Tech / Flow / Problem→Fix / Role 六段。
> 目的：项目演进记录 + 设计思路沉淀。

---

## 1. Qdrant Client 模块

**文件**：`backend/app/clients/qdrant.py`

### What
封装 Qdrant 向量数据库连接，提供 collection 管理（ensure / info）、向量 CRUD（upsert / search / delete）能力。单例 client，对外屏蔽 qdrant-client SDK 细节。

### Why
- 项目用 RAG 做知识库增强，必须有向量库存 embedding 后的文本向量
- 直接用 qdrant-client 会污染上层（业务逻辑和 SDK 耦合），封装后上层只关心「写入/查询」语义
- 单例模式避免每次请求都建连接（连接开销 + 端口资源）

### Tech Stack
- **qdrant-client 1.12.1**（官方 Python SDK）
- **Distance.COSINE**（余弦相似度，适合文本向量）
- **vector_size=1024**（与 DashScope text-embedding-v3 维度对齐）
- **HNSW 索引**（qdrant 默认，ANN 检索）

### Flow
```
ensure_collection() → 查 collection 是否存在 → 不存在则 create（VectorParams + COSINE）
upsert_points([PointStruct]) → client.upsert(wait=True) → 返回写入条数
search(query_vec, top_k) → client.search(limit, score_threshold, with_payload) → List[{id, score, payload}]
```

### Problem → Fix
- **维度不一致风险**：Qdrant collection 的 vector_size 必须与 embedding 模型输出维度完全一致（1024），否则 search 报 `Wrong dimensions` 错误
  - 解决：把 `VECTOR_SIZE = 1024` 和 `EMBEDDING_DIM = 1024` 都定义成模块级常量，加注释说明两边必须同步
- **get_collection_info 返回 vectors_count 为 None**：qdrant-client 1.12.x 的字段映射问题，实际数量在 `points_count`
  - 影响：仅展示用，不影响功能；先记下，后续可加补丁

### Architecture Role
属于 `clients/` 层（外部服务连接），按 §6 规则不写业务逻辑。被 `services/rag/pipeline.py` 调用做向量检索。

---

## 2. Embedding Client 模块

**文件**：`backend/app/core/embedding.py`

### What
封装 DashScope text-embedding-v3 模型，提供文本转向量的能力（单文本 + 批量）。复用 OpenAI SDK（DashScope 是 OpenAI 兼容协议）。

### Why
- RAG 第一步是 query → vector，必须有 embedding 客户端
- DashScope 的 OpenAI 兼容模式比直接调 dashscope SDK 更标准，未来换 provider 改动小
- 复用 `openai==1.58.1`（已经给 qwen 用），不需要新增依赖

### Tech Stack
- **openai 1.58.1**（OpenAI SDK + DashScope 兼容 base_url）
- **text-embedding-v3**（阿里最新 embedding 模型，中英文对齐好，1024 维）
- **encoding_format="float"**（默认输出 float 列表）

### Flow
```
embed_text("退款要多久？") → OpenAI(embeddings.create) → response.data[0].embedding → List[float] (len=1024)
embed_texts([...]) → 批量提交（一次 HTTP，省 RTT）→ List[List[float]]
```

### Problem → Fix
- **空字符串报错**：DashScope 对空 input 返回 400
  - 解决：入口加 `if not text: raise ValueError`；批量时把空串替换成 `" "`（一个空格）保留索引
- **API Key 占位符**：开发环境 `.env.dev` 默认是占位符，调用才报错，调试体验差
  - 解决：单例 client 第一次初始化时检测 `startswith("sk-put-your-real")` 直接抛 ValueError，错误信息指向 `.env.dev`

### Architecture Role
属于 `core/` 层（核心能力），按 §6 规则不调 HTTP API 路由、不做切片/不写 Qdrant。被 `services/rag/pipeline.py` 调用做 query 和文档的向量化。

---

## 3. RAG Pipeline 模块

**文件**：`backend/app/services/rag/pipeline.py`、`backend/app/services/rag/test_pipeline.py`

### What
实现 RAG 完整链路：`query → embed → qdrant.search → context 组装 → prompt → qwen LLM → answer`。提供 `run(query, top_k=5)` 一个函数对外。

### Why
- 解决「LLM 没有私有知识」的痛点 — 通过向量检索注入领域知识
- 编排层独立于 HTTP 层（services/ vs api/），后续可被不同入口复用（HTTP / Agent / 定时任务）
- system prompt 加约束「不知道就说不知道」，避免 LLM 编造答案

### Tech Stack
- **Pipeline 编排**：纯 Python 函数（同步）
- **检索**：Qdrant COSINE top-5
- **Prompt 模板**：`"基于以下内容回答问题：\n{context}\n\n问题：{query}"`，context 用 `[1]/[2]/[3]` 编号方便 LLM 引用
- **System prompt**：约束只基于参考资料，不知道就说不知道

### Flow
```
run(query) →
  1. embed_text(query)                          [core/embedding.py]
  2. qdrant_search(query_vec, top_k=5)          [clients/qdrant.py]
  3. _extract_text(payload) → contexts: List[str]
  4. _format_context() → "[1] xxx\n\n[2] yyy"
  5. messages = [system, user(prompt)]
  6. qwen_chat(messages, temperature=0.3)       [core/qwen.py]
  7. return {answer, contexts, scores}
```

### Problem → Fix
- **payload 结构不统一风险**：Qdrant 存的是 dict，业务要的是 str
  - 解决：`_extract_text()` 优先取 `payload["text"]`，fallback `payload["content"]`，再 fallback `""`，保持向后兼容
- **跨语言召回**：英文 query 想查中文知识库
  - 验证：test_pipeline 跑了 `How long is the refund process?` → 命中中文「退款流程」score 0.76 — text-embedding-v3 多语言对齐能力确认
- **知识库外的拒答**：测试「量子纠缠的物理机制」时，top score 仅 0.30（vs 命中知识库的 0.7+），LLM 看到低相关 context 后回答「我不知道」
  - 设计：system prompt 显式约束 + 低分 context 自然触发拒答，不需要额外加 score threshold 硬过滤

### Architecture Role
属于 `services/rag/` 编排层，是整个智能客服的「知识增强核心」。被 `api/chat.py` HTTP 端点调用，未来可被 Agent 工具链调用。

---

## 4. Chat HTTP API 模块

**文件**：`backend/app/api/chat.py`、`backend/app/schemas/chat.py`、`backend/app/main.py`（改）

### What
把 RAG pipeline 暴露成 HTTP `POST /chat` 端点。请求 `{"query": "..."}`，响应 `{"answer": "...", "contexts": [...], "scores": [...]}`。FastAPI 自动生成 Swagger 文档（`/docs`）。

### Why
- 前后端解耦：前端只关心 JSON 接口，不直接调 pipeline
- API 入口统一收口：超时、异常、参数校验都在 API 层做，pipeline 只关心业务
- Pydantic schema 自动校验 + 自动生成 OpenAPI，省去手写文档

### Tech Stack
- **FastAPI 0.115.6**（路由 + Pydantic 集成）
- **APIRouter**：按 §6 分层，chat 路由独立成 `api/chat.py`
- **asyncio.to_thread + wait_for(30s)**：pipeline 是同步阻塞 IO，丢到线程池避免阻塞 event loop；30s 超时保护
- **HTTPException**：标准错误码（400/500/504）

### Flow
```
HTTP POST /chat
  → FastAPI 解析 body → ChatRequest(query)  [schemas/chat.py]
  → Pydantic 自动校验（min_length=1, max_length=2000）
  → asyncio.wait_for(asyncio.to_thread(pipeline.run, query), timeout=30)
  → pipeline.run() 返回 {answer, contexts, scores}
  → ChatResponse 序列化 → JSON 200 OK
```

### Problem → Fix
- **同步 pipeline 阻塞 event loop**：FastAPI 是 async，如果直接 `pipeline.run(query)` 会卡住整个服务
  - 解决：`asyncio.to_thread` 把同步函数丢到默认 thread pool（任意 worker 线程），event loop 继续处理其他请求
- **慢请求拖垮服务**：embed + qdrant + LLM 三段加起来偶尔会超 5s
  - 解决：`asyncio.wait_for(timeout=30)`，超时返回 504 Gateway Timeout 而不是无限等待
- **main.py 残留旧 /chat**：之前 main.py 直接调 qwen_chat 的旧 /chat 会和新 router 冲突（重复路由）
  - 解决：删旧 /chat，main.py 只剩 `app.include_router(chat_router)` 一行，干净
- **CORS**：开发模式 `allow_origins=["*"]`，生产环境用具体域名（已通过 `APP_ENV` 环境变量切换）

### Architecture Role
属于 `api/` 层（HTTP 接口），按 §6 规则不写业务逻辑，只做参数解析 + 调 services。是前后端的唯一桥梁 — 前端 Vue3 之后会调这个端点。

---

## 整体架构图

```
┌──────────────────────────────────────────────────────────┐
│                    HTTP Layer (api/)                     │
│   POST /chat → api/chat.py → ChatRequest/Response       │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────┐
│               Orchestration (services/rag/)              │
│           pipeline.run(query) — 编排全链路              │
└──────┬──────────────────┬──────────────────┬─────────────┘
       │                  │                  │
       ▼                  ▼                  ▼
┌─────────────┐   ┌──────────────┐   ┌────────────────┐
│  core/      │   │  clients/    │   │   core/        │
│ embedding   │   │  qdrant      │   │   qwen (LLM)   │
│ (text-vec)  │   │  (vectors)   │   │   (generation) │
└─────────────┘   └──────────────┘   └────────────────┘
```

**设计原则**：
- `api/` 只做路由 → `services/` 编排 → `core/` 核心能力 / `clients/` 外部连接
- 每层只依赖下层，不跨层（如 api/ 不直接调 qdrant）
- 单例 client + 同步函数，编排层无状态，水平扩展友好

---

## 5. Ingest Pipeline 模块

**文件**：`backend/app/services/rag/ingest.py`、`backend/app/services/rag/test_ingest.py`

### What
知识库入库流水线。提供 `chunk_text()` 字符级滑动窗口切片 + `ingest_text()` 切→embed→upsert 全链路入库。同 source 二次入库幂等。

### Why
- 之前 RAG pipeline 只有「查」的能力，没有「灌」的能力；test_pipeline 是临时 seed 数据，不能算生产可用
- 切片是 RAG 质量的决定性因素之一（切太大切不细影响召回，切太碎丢上下文），需要一个统一入口
- 幂等性是工程刚需：同一文档误传两次不应该产生两份 vector，否则检索时被同一文本刷屏

### Tech Stack
- **字符级滑动窗口**：`chunk_size=500, overlap=50`（中文约 1 char ≈ 1.5 token，500 chars ≈ 一段）
- **uuid5 命名空间**：`uuid.uuid5(NAMESPACE_DNS, f"{source}:{i}")` → 同 source + 同 index 永远生成同一个 ID
- **qdrant upsert(wait=True)**：同步等待写入完成，避免 read-after-write 不一致
- **参数边界保护**：chunk_size ∈ [100, 2000]，overlap ∈ [0, chunk_size)，text ≤ 100KB

### Flow
```
ingest_text(text, source) →
  1. chunk_text(text)         → ["片1", "片2", ...]（滑动窗口切分）
  2. ensure_collection()       → collection 不存在则建
  3. embed_texts(chunks)       → List[List[float]]，批量一次 HTTP 省 RTT
  4. uuid5(source:0..N)        → 生成稳定 point_id
  5. PointStruct(id, vector, payload={text, source, chunk_index})
  6. qdrant.upsert(points)     → wait=True 同步等待
  7. return {ingested_chunks, source, chunk_ids, ...}
```

### Problem → Fix
- **chunk_size 太小导致大量碎片**（实测：50 chars/chunk 把 120 字符切成 3 片，每片只有一两个词，召回差）
  - 解决：定义 `MIN_CHUNK_SIZE=100`，比它小直接 ValueError；同时强制 `MAX_CHUNK_SIZE=2000`，避免单片超过 embedding 模型 8K token 上限
- **二次入库产生重复点**：同一 source 第二次入库会生成新的 uuid（如果用 uuid4），导致重复
  - 解决：用 `uuid5(NAMESPACE_DNS, f"{source}:{i}")`，输入相同 → 输出相同 → Qdrant upsert 是覆盖语义，自动幂等
- **payload 结构不统一**：test_pipeline seed 时用 `{text, source}`，ingest 用 `{text, source, chunk_index}`，pipeline 读 `payload["text"]` OK，但未来加字段容易踩坑
  - 当前处理：`_extract_text()` 优先读 `text` fallback `content`，保持向后兼容；后续新字段需在 ingest.py 集中定义
- **空 text 边界**：客户端可能传 `""` 或纯空白
  - 解决：`if not text.strip(): raise ValueError`，API 层 schema 加 `min_length=1` 双保险

### Architecture Role
属于 `services/rag/` 编排层（与 pipeline 同模块不同文件），是「写」侧；pipeline 是「读」侧。被 `api/admin.py` HTTP 端点调用，也支持 `python -m app.services.rag.test_ingest` 直接脚本调用灌测试数据。

---

## 6. Admin HTTP API 模块

**文件**：`backend/app/api/admin.py`、`backend/app/schemas/admin.py`、`backend/app/main.py`（挂载 router）

### What
把 Ingest 流水线暴露成 HTTP `POST /admin/ingest` 端点。请求 `{"text": "...", "source": "...", "chunk_size": 500, "overlap": 50}`，响应 `{"ingested_chunks": N, "source": "...", "chunk_ids": [...]}`。

### Why
- 让运营/开发能用 curl 灌知识库，不用进容器跑 Python
- 与 `/chat` 业务接口隔离（`/admin` 前缀），未来加鉴权中间件只需罩住 `/admin/*`
- HTTP 端点的 schema 校验 + 错误码 + 超时保护，让外部脚本（cron / 备份脚本）调用更可靠

### Tech Stack
- **APIRouter(prefix="/admin")**：路由前缀分组，方便未来 `/admin/knowledge` `/admin/users` 等扩展
- **Pydantic 边界保护**：text 1-100KB，chunk_size 100-2000，overlap 0-499，所有非法入参在 schema 层就被拦下（自动 422）
- **asyncio.wait_for(60s)**：入库涉及 embed + qdrant 写，比问答慢；给 60s 余量
- **错误码矩阵**：422（schema 校验）/ 400（业务参数）/ 500（内部）/ 504（超时）

### Flow
```
HTTP POST /admin/ingest
  → Pydantic 校验 IngestRequest
  → asyncio.to_thread(ingest_text, ...)  [线程池，不阻塞 event loop]
  → asyncio.wait_for(timeout=60s)
  → 返回 IngestResponse{ingested_chunks, source, chunk_ids, chunk_size, overlap}
```

### Problem → Fix
- **与 chat 共用 main.py 但要避免路由冲突**：之前的 chat router 已经注册 `/chat`，admin router 用 `/admin` 前缀天然隔离
  - 解决：APIRouter(prefix="/admin") + include_router 时不指定 path 前缀，路由完全独立
- **入库比问答慢得多**：embed_texts(5 chunks) ≈ 1s + qdrant upsert ≈ 0.05s + 排队 ≈ 5s
  - 解决：admin 端点独立 TIMEOUT=60s，比 chat 的 30s 宽
- **payload chunk_index 是新字段**：admin 入库的 payload 比 test_pipeline 多 `chunk_index`
  - 解决：在 ingest.py 集中定义 payload 结构（`{text, source, chunk_index}`），未来 `pipeline._extract_text()` 兼容提取即可
- **Swagger 中文 examples 显示乱码**：开发环境下 examples 字段中文在某些浏览器 console 会显示 unicode escape
  - 影响：仅 Swagger UI 展示美观性，不影响 API 功能；先记下，后续可加 `openapi_extra` 配置 encoding

### Architecture Role
属于 `api/` 层（与 chat router 同层），是「写」入口；`api/chat.py` 是「读」入口。两者职责对称，分别对应 RAG 的入库和检索两个方向。

---

## 更新后的整体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                      HTTP Layer (api/)                              │
│                                                                     │
│   POST /chat         POST /admin/ingest                             │
│        │                    │                                        │
│   api/chat.py    api/admin.py                                       │
│   (读入口)        (写入口)                                            │
└─────────┬──────────────────┬──────────────────────────────────────────┘
          │                  │
          ▼                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                Orchestration (services/rag/)                        │
│                                                                     │
│   pipeline.run(query)         ingest_text(text, source)              │
│   (检索 + 生成)                  (切片 + 入库)                         │
└──────┬──────────────────┬──────────────────┬─────────────────────────┘
       │                  │                  │
       ▼                  ▼                  ▼
┌─────────────┐   ┌──────────────┐   ┌────────────────┐
│  core/      │   │  clients/    │   │   core/        │
│ embedding   │   │  qdrant      │   │   qwen (LLM)   │
│ (text-vec)  │   │  (vectors)   │   │   (generation) │
└─────────────┘   └──────────────┘   └────────────────┘
```

**数据流向**：
- **读路径**：`/chat` → pipeline.run → embed + search + LLM → answer
- **写路径**：`/admin/ingest` → ingest_text → chunk + embed + upsert → Qdrant

**对称性设计**：read/write 两个入口在 api/ 和 services/rag/ 层完全对称，方便后续加 Admin Web UI 或读写分离。

---

## 7. Knowledge Management 模块

**文件**：`backend/app/services/rag/knowledge.py`、`backend/app/api/admin.py`（部分）、`backend/app/schemas/admin.py`（部分）

### What
知识库元数据查询与删除服务。提供 `get_info()` / `list_sources()` / `delete_by_source()` / `delete_by_ids()` 四个函数，对外暴露 4 个 HTTP 端点（`/admin/knowledge/*`），覆盖「看 + 删」两类运维需求。

### Why
- 之前只有 ingest（写）没有 knowledge 管理（查+删），运维只能进容器看 Qdrant REST API
- 「按 source 批量删」是高频操作：文档下线/重灌/纠错时，不可能逐个 chunk_id 删
- 暴露 HTTP 而不是 CLI：方便后续接 Admin Web UI，也方便运维脚本（cron + curl）调用

### Tech Stack
- **Qdrant scroll + Filter**：scroll 遍历所有点（分页，next_offset），Filter 做按字段过滤删除
- **payload 字段 `source`**：在 ingest 阶段就写入（`{text, source, chunk_index}`），后续所有按 source 的查询/删除都基于这个字段
- **delete 数量估算**：qdrant 的 `delete()` 不直接返回受影响行数，用删除前后的 `points_count` 差值
- **FieldCondition + MatchValue**：qdrant-client 的标准过滤构造方式

### Flow
```
GET /admin/knowledge/info
  → qdrant.get_collection_info() → {name, points_count, vector_size, status}

GET /admin/knowledge/sources
  → scroll 分批（limit=1000）+ 按 source 聚合 → [{source, count}]
  → 按 count 倒序

DELETE /admin/knowledge/source/{source}
  → before = get_collection_info().points_count
  → qdrant.delete(filter=source == X)
  → after = get_collection_info().points_count
  → return deleted = before - after

DELETE /admin/knowledge/points
  → qdrant.delete(point_ids=[...])
  → return deleted = len(point_ids)
```

### Problem → Fix
- **scroll 必须分页**：`scroll()` 单次返回最多 limit 条，没有 next_offset 时退出循环；如果忘记处理 offset 会漏数据
  - 解决：`while True: records, next_offset = client.scroll(...); ... if not next_offset: break`
- **delete 不返回行数**：qdrant-client 的 `client.delete()` 只返回 `OperationInfo`，没有 `affected_count`
  - 解决：删除前后各调一次 `get_collection_info()` 拿 points_count，差值即为删除数（极端情况下有竞态，但 1-2 个点的偏差可接受）
- **`vectors_count` 始终为 None**：qdrant-client 1.12.x 的字段映射 bug，实际数量在 `points_count`
  - 影响：仅展示字段，OpenAPI 文档标 Optional；info 端点照样返回 vectors_count=null
- **`source` 字段缺失的旧数据**：test_pipeline 早期 seed 的点虽然有 source，但如果将来用裸 qdrant upsert 没带 source，会被归到 `(unknown)` 组
  - 解决：list_sources 里默认 fallback `"source", "(unknown)"`，避免 KeyError；同时强烈约定 ingest 必须写 source
- **路径参数 source 校验**：URL `/admin/knowledge/source/""` 实际是路由不匹配（404），但如果 source 含特殊字符（如 `/`）会破坏 URL
  - 解决：FastAPI Path(..., min_length=1, max_length=200) 限制长度；URL 编码交给客户端

### Architecture Role
属于 `services/rag/` 编排层（与 pipeline / ingest 同模块），是知识库的「运维」入口；ingest 是「内容」入口，pipeline 是「查询」入口。三者构成知识库完整生命周期。

---

## 8. Chat History 多轮对话模块

**文件**：`backend/app/clients/redis_client.py`、`backend/app/services/chat_history.py`、`backend/app/services/rag/pipeline.py`（扩展）、`backend/app/schemas/chat.py`（扩展）、`backend/app/api/chat.py`（重写）

### What
实现 RAG 多轮对话。会话历史存在 Redis，每次 `/chat` 请求自动加载历史 + 拼接进 prompt + 持久化本轮问答。新增可选 `session_id` 入参 + 必返 `session_id` 出参，向后兼容单轮调用。

### Why
- 单轮 RAG 体验差：用户问「退款要多久？」→「1-3 个工作日」，再问「那没收到怎么办？」LLM 不知道上文指什么
- 多轮是客服场景刚需：电商客服 80% 的对话是连续追问
- Redis 适合：低延迟、自动 TTL、高读写并发；不需要关系查询，KV + List 足够
- 选择「可选 session_id」而非强制：保留单轮调用能力（避免破坏现有前端/调用方），让客户端按需启用

### Tech Stack
- **redis-py 5.2.1**（同步 SDK）+ asyncio.to_thread 异步化（与现有 pipeline 风格一致）
- **Redis 数据模型**：
  - Key：`chat:session:{uuid4_hex}`
  - Type：List
  - Value：JSON `{"role": "user"|"assistant", "content": "...", "ts": int}`
  - TTL：24 小时（EXPIRE 续期）
  - LTRIM 限长：MAX_HISTORY=20 条
- **Pipeline 调用**：`LPUSH + LTRIM + EXPIRE` 用 Redis pipeline 一次 RTT
- **Pipeline.run() 向后兼容**：新增 `history: Optional[List[Dict]] = None` 参数，`None/[]` 时走原单轮逻辑（PROMPT_TEMPLATE 里 history_block 替换为空字符串）
- **Prompt 模板扩展**：在 context 和 query 之间插入可选「对话历史」段，LLM 通过上下文理解指代

### Flow
```
POST /chat {query, session_id?}
  │
  ├─ session_id 缺失 → generate_session_id() (uuid4 hex)
  │
  ├─ history = load_history(session_id)   [Redis LRANGE + 反转]
  │
  ├─ result = pipeline.run(query, top_k=5, history=history)
  │     └─ 在 prompt 里：
  │         "基于以下内容回答问题：\n{context}\n对话历史：\n用户：...\n助手：...\n问题：{query}"
  │
  ├─ append_exchange(session_id, query, answer)  [Redis pipeline LPUSH+LTRIM+EXPIRE]
  │
  └─ return {answer, contexts, scores, session_id}
```

### Problem → Fix
- **Redis 连接初始化时机**：启动时 `ping()` 会 fail（如果 Redis 暂时不可用，整个 API 起不来）
  - 解决：懒加载 — `get_client()` 第一次调用时才建连接 + ping，FastAPI 启动不依赖 Redis
- **多轮 token 膨胀**：历史越长 prompt 越长，token 费越贵，且超出 LLM 上下文窗口
  - 解决：MAX_HISTORY=20（10 轮问答）+ LTRIM 自动截断；后续可加「超出则总结历史」逻辑（暂未实现）
- **中文 LLM 对指代理解**：测试「最低等级叫什么？」时 score 仅 0.58（知识库里没这句话）
  - 设计取舍：不强制让 RAG 召回历史相关内容，而是 prompt 注入历史让 LLM 自己结合上下文 → 测试中「最低等级叫普通会员」「最高等级叫钻石会员」都能正确答出
- **pipeline.run 单测破坏**：新增 history 参数后 test_pipeline 还在用 `rag_run("退款要多久？")` 调用
  - 解决：保持参数默认值 `history=None`，老调用完全不受影响（向后兼容的关键）
- **`Optional` 导入缺失**：第一次给 pipeline 加 history 参数时 `_format_history(history: Optional[List[...]])` 报 `NameError: Optional`
  - 解决：补 `from typing import Dict, List, Any, Optional`；FastAPI 启动时 uvicorn 直接报 traceback，问题定位很快
- **bash inline JSON + 中文编码丢失**：Windows Git Bash 下 `curl -d '{"query":"会员"}'` 会被 mojibake，导致 400 解析失败
  - 解决：所有中文 payload 写到 UTF-8 文件再 `--data-binary @file`；写测试脚本前先 Write 文件再 curl

### Architecture Role
属于跨模块集成：
- `clients/redis_client.py`：新增的外部服务连接（按 §6 分层）
- `services/chat_history.py`：services/ 编排层（与 pipeline 平级）
- `services/rag/pipeline.py`：在原 RAG 流程上加可选 history 注入（最小侵入式扩展，符合 §3「基于现有组件扩展，不重写」）
- `api/chat.py`：在 HTTP 层做 session_id 生命周期管理（不污染 pipeline）

至此后端实现了「读（chat pipeline）+ 写（ingest）+ 运维（knowledge）+ 状态（history）」四象限完整闭环。

---

## 更新后的整体架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         HTTP Layer (api/)                                    │
│                                                                             │
│   /chat (读+状态)              /admin/ingest (写)                             │
│        │                          │                                         │
│   api/chat.py                 api/admin.py                                   │
│        │           ┌──────────────┼──────────────┐                          │
│        │           │              │              │                          │
│        │           ▼              ▼              ▼                          │
│        │      /knowledge    /knowledge    /admin/ingest                     │
│        │      /info         /sources                                         │
│        │                    /source/{s}                                      │
│        │                    /points                                          │
└────────┼──────────────────────────┬──────────────────────────────────────────┘
         │                          │
         ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Orchestration (services/)                               │
│                                                                             │
│   rag/pipeline.run()         rag/ingest.ingest_text()      rag/knowledge.*() │
│   (读：检索+生成)              (写：切片+入库)                  (运维：查+删)  │
│                                                                             │
│   chat_history.*()  ─────────► Redis                                          │
│   (会话状态)                                                                 │
└──────┬──────────────────┬──────────────────┬──────────────────┬─────────────┘
       │                  │                  │                  │
       ▼                  ▼                  ▼                  ▼
┌─────────────┐   ┌──────────────┐   ┌────────────────┐   ┌──────────────┐
│  core/      │   │  clients/    │   │   core/        │   │  clients/    │
│ embedding   │   │  qdrant      │   │   qwen (LLM)   │   │  redis       │
│ (text-vec)  │   │  (vectors)   │   │   (generation) │   │  (sessions)  │
└─────────────┘   └──────────────┘   └────────────────┘   └──────────────┘
```

**四象限闭环**：
- **读**（pipeline）：query → vector → context → LLM → answer
- **写**（ingest）：text → chunk → vector → Qdrant
- **运维**（knowledge）：info / sources / delete（管理 Qdrant 内容）
- **状态**（chat_history）：session → Redis（多轮对话上下文）

**演进路径**：
1. 单轮 RAG（pipeline + chat）
2. 加 ingest（写）
3. 加 knowledge 管理（运维）
4. 加 chat_history（状态）
5. ✅ MySQL 持久化（冷路径真源）
6. 未来：Agent 工具调用 + 前端 UI

---

## 9. MySQL Persistence 模块

**文件**：`deploy/mysql/init/01_schema.sql`、`deploy/mysql/init/02_seed.sql`

### What
智能客服的 MySQL 持久层 schema。5 张表覆盖用户、会话、消息、知识库元数据、操作日志五大领域。Docker compose 启动时通过 `docker-entrypoint-initdb.d` 自动初始化（仅在数据卷为空时跑一次）。

### Why
- 之前所有数据都靠 Redis 24h TTL + Qdrant 永久，缺一块「结构化业务数据」的真源
- 用户、会话元数据、知识库运营元信息、审计日志 —— 这些都是「需要持久化 + 可查询 + 可审计」的典型关系数据
- MySQL 是常见工程实践考点（schema 设计 / 索引 / 字符集 / 逻辑删除 / 外键策略），单独抽出做模块便于维护
- 与 Redis 形成「热路径 + 冷路径」双层架构：Redis 缓存最新 20 条（低延迟 prompt 注入），MySQL 存全量（回填 + 审计）

### Tech Stack
- **MySQL 8.0**（Docker 镜像 `mysql:8.0`，端口 3307→3306 避开本机 MySQL）
- **字符集 / 排序规则**：`utf8mb4 / utf8mb4_unicode_ci`（mysqld 命令行已指定，表级 COLLATE 冗余声明以防环境漂移）
- **JSON 类型**：messages.contexts / messages.scores / operation_logs.detail（MySQL 5.7+ 原生 JSON，比 TEXT+JSON 函数更高效且可索引）
- **降序索引**：`KEY idx_conversations_user_status_time (user_id, status, last_message_at DESC)`（MySQL 8.0 新特性，避免 filesort）
- **自动时间戳**：`DEFAULT CURRENT_TIMESTAMP` + `ON UPDATE CURRENT_TIMESTAMP`（MySQL 5.6.5+ 才支持 DATETIME 默认值）
- **逻辑删除**：所有表统一 `deleted TINYINT NOT NULL DEFAULT 0`，业务层 `WHERE deleted=0` 过滤（不靠 DB 级 FK）

### 表设计矩阵

| 表 | 主键 | 关键唯一键 | 关键索引 | 与其他模块的关系 |
|---|---|---|---|---|
| users | id | username / email / phone | — | 未来 auth 模块管理；被 conversations / knowledge_documents / operation_logs 引用 |
| conversations | id | session_id | (user_id, status, last_message_at DESC) | session_id 与 Redis `chat:session:{id}` 共用；消息数 / 最后消息时间冗余存（避免每次 COUNT）|
| messages | id | — | (session_id, create_time) | RAG 检索结果 contexts/scores 存 JSON；Redis miss 时按 (session_id, create_time) 回填最近 20 条 |
| knowledge_documents | id | source | (status, doc_type) | source 与 Qdrant `payload.source` 对齐（幂等键）；Qdrant 存向量，本表存元数据 |
| operation_logs | id | — | (user_id, create_time), (action, create_time) | 审计专用；不存消息正文（消息正文在 messages） |

### Flow（未来业务集成路径）

```
┌──────────────────────────────────────────────────────────┐
│ 现有调用链（无 MySQL）                                     │
│   /chat → pipeline.run → Redis 取 history → LLM → Redis 写 │
└──────────────────────────────────────────────────────────┘
                         ▼  下一模块加 write-through
┌──────────────────────────────────────────────────────────┐
│ 加 MySQL 后（计划）                                         │
│   /chat → pipeline.run                                   │
│        ├─ Redis 取 history（miss 时从 messages 回填）       │
│        ├─ LLM 生成                                         │
│        ├─ Redis 写最新 20 条（LPUSH + LTRIM + EXPIRE）     │
│        └─ MySQL 写全量                                      │
│             ├─ INSERT messages (user/assistant)           │
│             ├─ UPSERT conversations SET message_count+=1 │
│             └─ INSERT operation_logs (action='chat')      │
└──────────────────────────────────────────────────────────┘
```

### Problem → Fix
- **MySQL UNIQUE 允许多个 NULL**：email/phone 可空 + UNIQUE 索引，MySQL 行为与 SQL 标准不同（允许多行 NULL），是想要的效果（一个用户没填邮箱也不冲突）
- **DATETIME 默认值报错风险**：低版本 MySQL 不支持 `DATETIME DEFAULT CURRENT_TIMESTAMP`
  - 解决：用 MySQL 8.0（docker-compose 已固定 `image: mysql:8.0`），并在所有时间字段显式声明
- **降序索引兼容性**：`KEY ... (col DESC)` 是 MySQL 8.0+ 才支持
  - 解决：依赖 MySQL 8.0（已固定），不需要兜底逻辑
- **JSON 字段 vs TEXT+JSON 函数**：本可以 `TEXT` + `JSON_EXTRACT()`，但 MySQL 原生 JSON 类型有：① 自动校验格式 ② 可建函数索引 ③ 存储更高效
  - 设计：3 个半结构化字段（contexts / scores / detail）都用 JSON，原生收益最大
- **密码 hash 预置的安全风险**：seed.sql 不预置任何明文/密文密码
  - 解决：`password_hash = '__SET_VIA_AUTH_MODULE__'` 占位，业务层识别到直接拒绝登录并提示「请通过管理后台重置密码」；首登密码改用 `docker exec + Python bcrypt` 脚本设置（见 seed.sql 注释）
- **外键策略**：DB 级 `FOREIGN KEY ... REFERENCES` 会强制级联删除/约束，对后期「逻辑删除 + 数据归档」不友好
  - 设计：所有外键用「逻辑外键」（直接存 id 字段），由业务层保证一致性；保留 `KEY ... (user_id, ...)` 索引维持查询性能

### Architecture Role
属于「基础设施层」（介于 docker-compose 服务定义和应用层之间）。按 CLAUDE.md §5 Scope Lock，本期只做 **schema + seed**；SQLAlchemy ORM / repository / service 是下一模块，不在本期范围。被以下未来模块消费：
- **Auth 模块**：users 表的注册/登录/密码重置
- **会话持久化模块**：conversations + messages 表的 write-through 写入 + Redis miss 回填
- **知识库元数据模块**：ingest 时同时写 Qdrant + knowledge_documents；knowledge 管理端点返回元数据
- **审计模块**：operation_logs 记录所有写操作

### 演进路径更新
- ✅ 后端 8 模块完成（读 / 写 / 运维 / 状态 四象限）
- ✅ MySQL 持久层 schema 落地（用户 / 会话 / 消息 / 元数据 / 审计）
- ✅ SQLAlchemy ORM + Auth 闭环（10a + 10b 一次到位）
- ⏳ 会话 write-through（pipeline 加 MySQL 写入，把 Redis 热路径 + MySQL 冷路径串起来）
- ⏳ 前端 Vue3 + Agent 工具调用

---

## 10. MySQL ORM + Auth 模块（10a + 10b 一次到位）

**文件**：
- `core/config.py`、`core/security.py`
- `clients/mysql_client.py`
- `models/{base,user,conversation,message,knowledge_document,operation_log}.py`
- `schemas/auth.py`、`services/auth_service.py`
- `api/deps.py`、`api/auth.py`
- 改：`main.py`、`api/admin.py`、`api/chat.py`

### What
把 §9 的 MySQL schema 用起来 + 完整用户认证体系。
- **ORM 层**：SQLAlchemy 2.0 sync + PyMySQL + 5 个 ORM model + `get_db()` Depends
- **Auth 层**：bcrypt 密码哈希 + JWT (HS256, 24h) + httpOnly Cookie + 5 个端点
- **保护层**：`/admin/*` 全部加 `Depends(require_admin)`，`/chat` 加可选 user 上下文

### Why
- §9 只建了表，没有 ORM 就没法在 Python 里增删改查
- Auth 是几乎所有后续模块的前置（admin 鉴权、会话归属、审计 user_id）
- /admin/* 之前是裸奔的 — 任何能访问 8000 端口的人都能灌库 / 删库，必须加保护

### Tech Stack
- **SQLAlchemy 2.0.36**：新式 `Mapped[T] + mapped_column()` 类型化声明，`select().where()` 新查询语法
- **PyMySQL 1.1.1**：同步 MySQL 驱动，配合 `cryptography` 43.0.3 支持 MySQL 8 caching_sha2_password
- **bcrypt 4.2.1**：默认 rounds=12，~250ms/次（足够抵御暴力破解）
- **python-jose[cryptography] 3.3.0**：JWT 签发与解码，HS256 对称算法（适合单体后端；多服务时换 RS256）
- **python-multipart 0.0.20**：`OAuth2PasswordRequestForm` 依赖，需要 form-data 解析
- **pydantic-settings 2.7.0**：统一读 env，自动注入环境变量
- **Cookie**：`HttpOnly + SameSite=Lax`，生产用 `Secure`（HTTPS only）
- **同步 vs 异步**：选同步 SQLAlchemy，跟现有 redis_client 风格统一；DB 操作 `asyncio.to_thread` 异步化

### 接口矩阵

| 端点 | 方法 | 鉴权 | 入参 | 出参 |
|---|---|---|---|---|
| /auth/register | POST | 公开 | username / password / display_name? / email? | UserOut (201) |
| /auth/login | POST | 公开 | OAuth2 form: username / password | LoginResponse + Set-Cookie |
| /auth/logout | POST | 公开 | — | 清 cookie |
| /auth/me | GET | 必登录 | — | UserOut |
| /auth/change-password | POST | 必登录 | old_password / new_password | message |
| /admin/ingest | POST | **require_admin** | IngestRequest | IngestResponse |
| /admin/knowledge/* | GET/DELETE | **require_admin** | — | ... |
| /chat | POST | 可选登录 | ChatRequest | ChatResponse（含 user_ctx 日志）|

### 数据流

```
                    ┌─────────────────────────────────────┐
                    │  /auth/login (OAuth2 form)          │
                    │  → authenticate()                   │
                    │  → bcrypt.checkpw                   │
                    │  → create_access_token(user_id)     │
                    │  → response.set_cookie(cs_token)    │
                    └───────────────┬─────────────────────┘
                                    │ Set-Cookie
                                    ▼
┌──────────────────────────────────────────────────────────────────┐
│ 后续请求带 Cookie: cs_token=<JWT>                                 │
│   → _extract_token(req.cookies)                                 │
│   → jwt.decode(JWT_SECRET, HS256)                               │
│   → user_id = payload["sub"]                                    │
│   → get_user_by_id(db, user_id)                                 │
└──────────────────────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────────┐   ┌────────────────────┐   ┌──────────────────────┐
│  /admin/*         │   │  /chat (可选)      │   │  /auth/me (必登录)   │
│  require_admin    │   │  get_current_user  │   │  get_current_user    │
│  ↓ user.role=     │   │  _optional → None  │   │  ↓ 401 if None       │
│     'admin'       │   │  日志含 user_ctx   │   │                      │
│  ↓ 403 if not     │   │                    │   │                      │
└───────────────────┘   └────────────────────┘   └──────────────────────┘
```

### Problem → Fix
- **`--data-binary @file` 文件末尾 `\n` 导致 OAuth2PasswordRequestForm 解析多一个空 password field**（最坑的 bug，调试 30 分钟才发现）
  - 现象：宿主机 curl `/auth/login` 返回 401；容器内 curl OK；手测 bcrypt True
  - 根因：`Write` 工具自动加 trailing `\n`，python-multipart 解析 `password=TestPass123\n` 时把 `\n` 当作额外 field value 起点
  - 解决：用 `printf "..." > file` 替代 `Write` 工具（不自动加换行）；或者用 `python -c` 生成文件
  - 教训：**写测试 payload 文件一定要先 `xxd` 看字节**；推荐用 `printf` 而不是 `Write`
- **Cookie 名字碰撞**：用 `cs_token` 避免与项目其他 cookie 撞名
- **bcrypt rounds 选择**：12 是 bcrypt 库默认（OWASP 推荐 ≥ 10）；rounds=14 哈希时间 ~1s（用户体验差），rounds=10 哈希 ~100ms（弱）
- **同步 SQLAlchemy vs 异步 aiomysql**：选同步，跟 redis_client 风格统一；改动最小化（不改 docker-compose.yml 的 DATABASE_URL）
- **password_hash 占位符防登录**：seed.sql 预置的 admin 密码 hash 是 `__SET_VIA_AUTH_MODULE__`；`authenticate()` 看到这个值直接返回 None，避免「用占位 hash 能登录」的安全漏洞
- **依赖关系**：bcrypt 4.x 是稳定版；python-jose 已不维护但 FastAPI 文档示例在用，保留；SQLAlchemy 2.0.36 是 2.0.x 最新稳定
- **启动时建表兜底**：`Base.metadata.create_all` 仅在表不存在时建；schema 已存在（§9 docker MySQL 已建）所以是 no-op，但保证未来 dev 环境冷启动能 work

### Architecture Role
属于「基础设施 + 鉴权」双层集成：
- **新增 models/ 层**：扩展 CLAUDE.md §6 原有分层（api/services/core/rag/clients/schemas/utils），新增 domain entity 层；ORM model 是横切关注点
- **新增 api/deps.py**：把 `get_current_user` / `require_admin` 抽出来复用，不写在每个 endpoint 里
- **跨模块集成**：
  - `core/config.py` 集中读 env（之前散落在 `os.getenv()` 各处）
  - `api/admin.py` 改造：从裸奔 → admin 鉴权
  - `api/chat.py` 增强：可选 user 上下文（日志含 user_ctx，为未来 MySQL write-through 留接口）
  - `main.py` 加 `Base.metadata.create_all` 启动兜底 + shutdown 时 `close_engine()` 优雅关闭

### 演进路径更新
- ✅ 后端 9 大模块 + Auth 闭环
- ✅ MySQL 从「有表无 ORM」升级为「有表 + ORM + Auth」
- ✅ Write-Through 闭环（§11）：Redis 热路径 + MySQL 冷路径 + operation_logs 审计
- ⏳ 前端 Vue3 + Agent 工具调用

---

## 11. Write-Through 闭环模块（§11 a/b + audit）

**文件**：
- 新增 `services/audit_service.py`
- 改 `services/chat_history.py`、`services/rag/ingest.py`、`services/rag/knowledge.py`
- 改 `api/chat.py`、`api/admin.py`、`schemas/admin.py`

### What
把 §10 的 MySQL 「有 ORM 不写」推进为「write-through 实时双写」 + 完整审计。
- **会话 write-through（11a）**：Redis 写后立即 MySQL（messages + UPSERT conversations）
- **Redis miss 回填**：24h TTL 过期后从 MySQL 按 create_time DESC 拉最近 20 条
- **知识库 write-through（11b）**：Qdrant upsert 后立即 MySQL（knowledge_documents 元数据）
- **删除软删**：delete_by_source 同步 UPDATE status=0（保审计完整性）
- **audit 上报**：chat / ingest / delete_knowledge 三个动作全部写 operation_logs

### Why
- Redis 是热路径（24h TTL，最新 20 条），但「会话」是用户的资产生命周期，不该过期丢失
- MySQL 是冷路径真源，永不过期、可查历史、可做数据分析 / 合规审计
- 「运维只需要看 Qdrant」的旧模式不够：管理员要能查「哪个 source 是什么时候谁传的」，需要元数据表
- 审计是几乎所有企业项目的合规刚需（GDPR / 等保 / SOC2）

### Tech Stack
- **write-through 模式**：Redis 写后调独立 session 写 MySQL（不共享事务，避免 MySQL 挂拖垮 Redis 热路径）
- **MySQL UPSERT**：Python 层 SELECT + INSERT/UPDATE（不用 dialect-specific `on_conflict_do_update`，依赖少、可移植）
- **软删 vs 硬删**：knowledge_documents 用 status=0（保元数据可查）/ messages 用 deleted=0（业务删）
- **独立 audit session**：`get_session_local()()` 每次新建会话，避免污染调用方 db 事务
- **audit best-effort**：`try_log_action` 异常仅 warning，不抛（审计不能影响主流程）
- **客户端 IP 取自** `X-Forwarded-For` 优先（反代场景），fallback `request.client.host`

### 数据流（write-through）

```
                          ┌─────────────────────────────────────────┐
                          │         HTTP 请求 (admin / user)         │
                          └────────────┬────────────────────────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        ▼                              ▼                              ▼
┌────────────────┐            ┌────────────────┐            ┌────────────────┐
│ Redis 热路径   │            │ Qdrant 真源    │            │ MySQL 冷路径    │
│ chat:session:X │            │ payload.text   │            │ messages       │
│ TTL=24h        │            │ payload.source │            │ conversations  │
│ MAX_HISTORY=20 │            │ uuid5 幂等     │            │ knowledge_doc  │
└────────┬───────┘            └────────┬───────┘            │ operation_logs │
         │                             │                     └───────┬────────┘
         │ 写完即返                    │ 写完即返                    │ 独立事务
         │ (热路径优先)                │ (Qdrant 已 upsert)         │ (失败仅 warn)
         │                             │                             │
         ▼                             ▼                             ▼
    client 200                    client 200                     后台异步
```

### 接口变更

| 端点 | 变化 |
|------|------|
| POST /chat | + write-through MySQL（messages + UPSERT conversations）+ audit `chat` |
| POST /chat | load_history → load_history_with_fallback（Redis miss → MySQL 回填） |
| POST /admin/ingest | + `title` / `description` 入参；+ `uploader_id=admin.id`；+ audit `ingest` |
| DELETE /admin/knowledge/source/{s} | + MySQL status=0 软删；+ audit `delete_knowledge` |
| 所有端点 | + audit 上报（含 IP / UA / result / error_msg）|

### Problem → Fix
- **MySQL 写失败不能影响主流程**：会话写入失败会让 /chat 504 是错误设计
  - 解决：`persist_to_mysql` 内部 `try/except` + `rollback()`，失败 `logger.warning` 不抛
- **匿名 user_id 表达**：schema `user_id BIGINT NOT NULL` 不允许 NULL
  - 设计：约定 `user_id=0` 表示匿名（不在 DB CHECK 约束，避免 schema 限制）
  - 影响：未来查「匿名用户的所有消息」用 `WHERE user_id=0`
- **audit session 独立性**：与主请求共享 session 会让 audit 失败影响主事务
  - 设计：`try_log_action` 内部新建 `get_session_local()()`，finally 关闭
- **Redis miss 回填顺序**：MySQL 可能写入慢，按 create_time DESC 而非 ASC（最新优先）
  - 注意：`LIMIT 20` 后反转成正序，与 Redis 返回顺序一致
- **delete 软删 vs 硬删**：元数据是 admin 运营参考，不该随 Qdrant 一起消失
  - 解决：`UPDATE knowledge_documents SET status=0`（status=0 在 list_sources API 里默认过滤掉）
- **ingest 元数据 uploader_id 来源**：admin user.id 已经在 Depends(require_admin) 拿到
  - 透传：`ingest_text(..., admin.id, payload.title, payload.description)` 一路传到 services
- **审计 IP 不可信**：直接 `request.client.host` 在反代后是 127.0.0.1
  - 解决：优先读 `X-Forwarded-For`，第一段是真实 IP；fallback client.host
- **audit JSON 字段过长**：detail 字段是 JSON，无显式长度限制
  - 当前：依赖 MySQL JSON 类型（最大 64KB），超长会报 DataError → 被 try/except 吞掉
  - 后续：可加 `if len(json.dumps(detail)) > 60000: truncate`

### Architecture Role
跨多个现有模块的集成：
- `services/chat_history.py`：从「只管 Redis」升级为「Redis + MySQL 双层」（同 service 不分拆，避免两层各自管事务）
- `services/rag/ingest.py` / `knowledge.py`：从「只管 Qdrant」升级为「Qdrant + MySQL 元数据」
- `services/audit_service.py`：新独立 service，best-effort 写 operation_logs
- `api/chat.py`：从「调 chat_history」升级为「调 chat_history（含 MySQL）+ audit」
- `api/admin.py`：从「透传 admin」升级为「透传 admin + title/description + audit」

### 演进路径更新
- ✅ 后端 10 大模块 + Auth + Write-Through 闭环
- ✅ 会话：Redis 24h 热路径 + MySQL 永不过期冷路径
- ✅ 知识库：Qdrant 向量 + MySQL 元数据
- ✅ 审计：chat / ingest / delete_knowledge 全部上报
- ✅ 会话列表 API + 分页优化（§12 / §13）
- ✅ SSE 流式输出 + Vue3 前端（§14）
- ⏳ Admin 知识库管理页前端
- ⏳ Agent 工具调用（LangGraph）

---

## 12. Conversation List API 模块

**文件**：`backend/app/api/conversations.py`、`backend/app/schemas/conversation.py`

### What
新增会话元数据查询 API。3 个端点覆盖前端「会话列表 + 历史消息 + 删除会话」三类核心需求：
- `GET /conversations` — 当前登录用户的所有会话（带 last_message 预览 + message_count）
- `GET /conversations/{session_id}/messages?limit=20&cursor=` — 单会话的消息分页（cursor 游标）
- `DELETE /conversations/{session_id}` — 软删会话（消息级联软删）

### Why
- §11 写完 MySQL 后，前端还缺「能看到我之前聊过什么」的能力；之前只有 Redis 24h 热数据
- 「按时间倒序列出会话」是几乎所有聊天产品的标准交互（ChatGPT / 飞书 / 钉钉都这样）
- 会话列表的 N+1 问题在 ORM 写法下极容易踩坑（每条会话查一次 last_message），单模块抽出做优化示例

### Tech Stack
- **SQLAlchemy ORM**：复用 §10 的 models，复用 `get_db()` Depends
- **GROUP BY + JOIN 子查询**：避免 N+1 一次拉完 last_message（详见 §13 优化）
- **Cursor-based 分页**：用自增 id 作游标（稳定 + 抗软删），不用 offset/limit（offset 在大数据下退步）
- **fetch limit+1 检测 has_more**：经典游标分页 trick，比 COUNT(*) 快
- **软删**：DELETE 接口只 UPDATE `status=0`（保审计 + 可恢复），不真删 messages

### Flow

```
GET /conversations
  → user = Depends(get_current_user)             [api/deps.py]
  → db.query(Conversation)
       .filter(user_id=user.id, status=1, deleted=0)
       .order_by(Conversation.last_message_at DESC)
  → 返回 {conversations: [...], total: N}

GET /conversations/{session_id}/messages?limit=20&cursor=12345
  → 校验 session_id 归属当前 user
  → q = db.query(Message).filter(session_id=X, deleted=0)
       .order_by(Message.id DESC)
  → if cursor: q = q.filter(Message.id < cursor)
  → records = q.limit(limit+1).all()             [多取一条]
  → has_more = len(records) > limit
  → if has_more: records = records[:limit]
  → next_cursor = records[-1].id if has_more else None
  → 返回 {session_id, messages, has_more, next_cursor, limit}

DELETE /conversations/{session_id}
  → 校验归属
  → UPDATE conversations SET status=0, deleted=1 WHERE session_id=X
  → UPDATE messages SET deleted=1 WHERE session_id=X   [级联软删]
  → 写 operation_logs audit
  → return {session_id, message: "已删除"}
```

### Problem → Fix
- **MySQL 不支持 `NULLS LAST`**：SQLAlchemy `.nulls_last()` 生成的是 PostgreSQL 方言，MySQL 8.0 会报错
  - 解决：直接去掉 `.nulls_last()`，MySQL 的 `DESC` 排序天然把 NULL 放最后（与 PG 行为差异）
  - 教训：**ORM 写法要考虑方言差异**，跨 DB 时小心方言函数
- **`last_message` 取错（取到用户 query 而非 assistant 回答）**：同一事务内 user 和 assistant 两条 message 的 `create_time` 精确到秒可能相同，按 `create_time DESC` 取 first 不稳定
  - 解决：改用自增 `id DESC`（assistant 总在 user 之后插入，id 一定更大）
- **N+1 查询**：用 `for conv in conversations: msg = db.query(Message)...` 时 1 次查会话 + N 次查最后消息，QPS 100 时数据库被打爆
  - 解决：详见 §13
- **匿名用户 session_id 撞库**：未登录用户所有请求共享 `user_id=0`，跨用户能互相看到对方会话
  - 解决：conversations 关联表 schema 已经强制 `user_id BIGINT NOT NULL`；查询时必须加 `user_id` 过滤；删除时校验 `session.user_id == current_user.id`
- **Cursor 分页边界**：用 last_id 当 next_cursor 时，last record 要保留在当前页（不能 pop）
  - 解决：`records = q.limit(limit+1).all()` 多取一条，`has_more = len > limit`，截断到 limit（不丢数据）

### Architecture Role
属于 `api/` 层（与 chat / admin router 平级），复用 §10 的 `get_db` + `get_current_user` Depends，**不引入新 service 层**（纯查询层够简单，service 化反而冗余）。被前端 Vue3 §14 直接调用。

---

## 13. Conversation 分页 + N+1 优化模块

**文件**：`backend/app/api/conversations.py`（同 §12，单模块二次迭代）

### What
两个优化点叠加：
1. **N+1 消除**：`/conversations` 列表的 `last_message` 用 `GROUP BY + JOIN 子查询` 一次拉完
2. **Cursor 化**：`/messages` 已有 cursor；补 `has_more` 检测 + `next_cursor` 返回值

### Why
- 列表 N+1 是 ORM 经典坑：10 条会话 = 11 次查询；100 条 = 101 次；用户量上去后数据库 CPU 直接打满
- Cursor 分页是聊天场景的「正确」做法：offset 在 1000 万条数据后 LIMIT 1000000, 20 要扫 100 万行（巨慢），cursor 用 id 直接定位 O(log n)
- 两个改动同文件、同函数、同 schema，做一次模块化说明比拆成两节更连贯

### Tech Stack
- **JOIN 子查询**：
  ```sql
  SELECT c.*, m.content AS last_message
  FROM conversations c
  LEFT JOIN (
    SELECT session_id, content
    FROM messages
    WHERE id IN (SELECT MAX(id) FROM messages GROUP BY session_id)
  ) m ON c.session_id = m.session_id
  WHERE c.user_id = ? AND c.status = 1 AND c.deleted = 0
  ORDER BY c.last_message_at DESC
  ```
- **fetch limit+1 trick**：
  ```python
  records = q.limit(limit + 1).all()           # 多取 1 条
  has_more = len(records) > limit
  if has_more: records = records[:limit]       # 截断
  next_cursor = records[-1].id if has_more else None
  ```
- **SQLAlchemy 写法**：用 `subquery()` + `aliased()` + `in_()` 表达子查询，避免原生 SQL 字符串拼接

### Flow 优化对比

| 维度 | 旧实现（N+1） | 新实现（GROUP BY） |
|------|--------------|-------------------|
| 100 条会话 | 101 次查询 | 1 次查询 |
| 数据库 QPS | 线性增长 | 恒定 |
| 代码复杂度 | 简单但慢 | 子查询但快 |
| 维护性 | 容易踩坑 | 一次写对复用 |

### Problem → Fix
- **`AttributeError: 'str' object has no attribute 'content'`**：子查询返回 `row.content` 是字符串，不是 Message ORM 对象；消费者误调 `.content`
  - 根因：JOIN 子查询只 SELECT 了 `content` 字段，没有完整 Message ORM
  - 解决：consumer 直接 `last_msg_map.get(r.session_id)` 拿字符串，不要 `.content`
- **`GROUP BY` 与 SQL 模式不匹配**：MySQL `ONLY_FULL_GROUP_BY` 模式下，`SELECT c.*, m.content` 配合 `GROUP BY c.session_id` 会报「select 列表不在 group by 中」
  - 解决：把 GROUP BY 放在子查询里（`SELECT session_id, content FROM messages WHERE id IN (SELECT MAX(id) FROM messages GROUP BY session_id)`），外层 LEFT JOIN 不需要 GROUP BY
- **Cursor 越界**：`cursor=0` 或 `cursor=None` 时要分别处理
  - 解决：`if cursor is not None: q = q.filter(Message.id < cursor)`，不传 cursor 走最新页
- **LIMIT 边界**：limit=0 会返回空但 has_more 判断出错
  - 解决：API 层 `limit = max(1, min(100, limit))` 强制 [1, 100]

### Architecture Role
属于 §12 同模块的二次迭代，体现「先跑通再优化」的开发节奏：§12 先实现功能，§13 再针对性能/正确性打磨。**没有拆成新文件**，因为改的是同一个函数（list_conversations / get_messages），拆文件反而增加导航成本。

---

## 14. SSE 流式输出 + Vue3 前端模块

**文件**：
- 后端：`backend/app/core/qwen.py`（新增 `stream_chat()`）、`backend/app/services/rag/pipeline.py`（`run()` → `run_stream()`）、`backend/app/api/chat.py`（JSON → `StreamingResponse`）
- 前端（新增）：`frontend/package.json` / `vite.config.ts` / `tsconfig.json` / `index.html` / `src/main.ts` / `src/style.css` / `src/types.ts` / `src/api.ts` / `src/App.vue` / `src/components/{LoginForm,ChatPage,ConversationList,MessageList,MessageInput}.vue`

### What
把系统从「后端 demo」升级为「可用聊天产品」。两个独立但强耦合的子模块：

**14a. 后端 SSE 流式（最小侵入式扩展）**
- `core/qwen.py` 加 `stream_chat()` 生成器：`client.chat.completions.create(..., stream=True)` → yield 每个 `delta.content`
- `services/rag/pipeline.py` 把 `run()` 改成 `run_stream()`：先 yield meta（contexts+scores），再 yield 每个 token，最后 yield done（完整答案）
- `api/chat.py` 把 `POST /chat` 改成 `StreamingResponse(text/event-stream)`，自定义 JSON 事件协议

**14b. Vue3 前端（全新模块，零后端耦合）**
- Vue 3.5 + Vite 6 + TypeScript 5.7（**不用 Pinia/Redux**，ref + emit 足够）
- 左侧会话栏 + 右侧聊天窗口 + 输入框，5 个组件拆分
- 自研 SSE 客户端：`fetch + ReadableStream + TextDecoder`，**不用 EventSource**（要支持 POST body）
- Vite 代理同源开发（5173 → 8000），避开 CORS + 简化 cookie 传递

### Why
- 流式是真流式（不是 fake typing），LLM 每个 token 立刻推到浏览器，体感从「等 5s 全出」变成「点完就出字」
- 不重构后端架构（CLAUDE.md §2 禁止）：只在 `qwen.py / pipeline.py / chat.py` 三个文件加 stream 路径，**不删旧的非流式实现**（向后兼容）
- 不引入复杂状态库：项目规模不需要 Pinia；ref + emit 跨组件传值够用
- 不引入 UI 框架：scoped CSS 手写，避免 Element Plus 等重组件库（与 CLAUDE.md §1「最小可运行」原则一致）

### Tech Stack

| 层 | 选型 | 理由 |
|----|------|------|
| 前端框架 | Vue 3.5 + Composition API | CLAUDE.md §6 强制 `<script setup lang="ts">` |
| 构建工具 | Vite 6.4 | 启动快 + HMR 流畅 + TS 原生支持 |
| 类型 | TypeScript 5.7 strict | 零 `any`，所有 API 响应都 typed |
| HTTP 客户端 | fetch + ReadableStream | 流式要 chunked transfer，axios 不友好 |
| 状态管理 | ref + emit + props | 不引入 Pinia（5 个组件规模不需要） |
| 路由 | 无 | 单页应用，直接条件渲染 |
| UI | scoped CSS 手写 | 无 UI 框架依赖（CLAUDE.md 强调不过度设计） |
| SSE 协议 | `data: {json}\n\n` | 标准 text/event-stream 格式 |
| 反向代理 | Vite dev proxy | 同源开发，无 CORS 烦恼 |

### 自定义 SSE 事件协议

| event.type | payload | 触发时机 | 前端处理 |
|------------|---------|----------|----------|
| `meta` | `{contexts: string[], scores: number[]}` | RAG 检索完成 | 缓存到 capturedContexts |
| `token` | `{text: string}` | LLM 每个 delta.content | 累加到 streamingText（光标逐字显示） |
| `done` | `{session_id: string}` | 全文生成 + write-through 完成 | 固化为 assistant 消息 + 刷新会话列表 |
| `error` | `{message: string}` | 异常中断 | 红色错误条 + 清空 streamingText |

### Flow（端到端）

```
用户输入"什么是退款政策"
  │
  ▼
ChatPage.sendMessage(text)
  │ 1) 乐观插入 user 消息
  │ 2) streaming = true, streamingText = ""
  │
  ▼
api.streamChat(text, sessionId)  [AsyncGenerator]
  │ fetch POST /chat (credentials: include)
  │ reader = res.body.getReader()
  │ buffer = ""
  │
  │ while not done:
  │   chunk = await reader.read()
  │   buffer += decoder.decode(chunk)
  │   for part in buffer.split("\n\n"):
  │     if part.startsWith("data:"):
  │       yield JSON.parse(part[5:])
  │
  ▼
后端 /chat 端点
  │ pre-load history (Redis → MySQL fallback)
  │ def event_generator():           [sync generator in thread]
  │   for type, data in rag.run_stream(query, history):
  │     if type == "meta":   yield "data: {meta json}\n\n"
  │     if type == "token":  yield "data: {token json}\n\n"
  │     if type == "done":
  │       Redis 写 history
  │       MySQL 写 messages + UPSERT conversations
  │       audit 写 operation_logs
  │       yield "data: {done json}\n\n"
  │
  ▼ return StreamingResponse(event_generator, media_type="text/event-stream")
  
  │
  ▼ (前端的 for await 循环)
ChatPage 收到 token 事件 → streamingText += text → 自动滚动
ChatPage 收到 done 事件 → 固化 assistant 消息 + 刷新左侧会话列表
```

### 关键设计决策

| 决策 | 备选 | 选择 | 理由 |
|------|------|------|------|
| 浏览器 SSE 客户端 | EventSource vs fetch+ReadableStream | **fetch+ReadableStream** | EventSource 不支持 POST body，chat 需要带 `query + session_id` |
| 流式写入策略 | 全程流式写 MySQL vs 完整体再写 | **完整体再写** | 1) 写 MySQL 不能半截 2) write-through 失败要可重试 3) 简单可靠 > 复杂实时 |
| 状态管理 | Pinia vs ref+emit | **ref+emit** | 5 个组件规模，Pinia 反而是 over-engineering |
| UI 框架 | Element Plus vs 手写 | **手写** | 项目演示性质，UI 简单，手写更可控 |
| 后端流式函数 | 新增 stream 版本 vs 改原 run | **改 run** | 唯一调用方是 /chat，改原函数最小侵入（CLAUDE.md §3） |
| 写 MySQL 时机 | 收到 token 即写 vs done 才写 | **done 才写** | 同上，事务完整性 + 失败可重试 |
| 同步 vs 异步 generator | async def vs def | **def（同步）** | FastAPI 自动把 sync generator 跑在 threadpool；与现有 sync pipeline 风格统一 |
| Vite 代理 | 同源代理 vs CORS 配置 | **同源代理** | dev 体验最简；生产部署时由 nginx 反代同样思路 |

### Problem → Fix
- **EventSource 不支持 POST**：`POST /chat` 必须带 `{"query": ..., "session_id": ...}`，EventSource 只能 GET
  - 解决：手写 `fetch + ReadableStream` 解析器，buffer 用 `\n\n` 分包（标准 SSE 协议）
- **Windows Bash 中文 JSON 编码丢失**：`curl -d '{"query":"退款"}'` 实际发送的是 mojibake（GBK 被当 UTF-8 解释）
  - 解决：所有中文 payload 先 `printf > /tmp/file.json` 再 `curl --data-binary @file`
  - 这是已记录的 Windows 特性（CLAUDE.md 全局约束里有 `feedback_windows_bash_utf8` memory）
- **MySQL `NULLS LAST` 不支持**：上一次的坑再次踩到，这次用 `id DESC` 绕开（id 自增稳定，非 NULL）
- **`Write` 工具自动加 trailing `\n` 破坏 SSE**：调试时想把 SSE 输出写到文件分析，Write 的 `\n` 会让 SSE 解析器误判
  - 解决：调试一律用 `curl -N > /tmp/output.sse`（curl 不加 trailing newline）
- **Vite 代理下 cookie 透传**：默认 Vite proxy 不会自动带 cookie
  - 解决：`vite.config.ts` 的 proxy 配置里加 `changeOrigin: true` + `cookieDomainRewrite: 'localhost'`
- **流式响应必须关闭 nginx buffer**：本地 dev 用 vite proxy 没事；生产 nginx 必须加 `proxy_buffering off` 否则客户端要等 LLM 全部生成完才收到
  - 当前：dev 阶段不部署，记录到部署 TODO
- **chat completion stream=True + AsyncOpenAI 兼容性**：同步 OpenAI 客户端的 stream 模式正常工作，但要把整个 for 循环包在 `try/except` 里（网络中断会抛）
  - 解决：generator 用 try/finally 兜底 `reader.releaseLock()`，避免连接泄漏
- **乐观插入 user 消息后网络失败**：用户消息已经渲染在聊天区，但后端没收到
  - 当前：失败时弹错误条 + 保留 user 消息（用户可手动重发）；没做「失败消息标红」复杂状态机
  - 设计取舍：错误重试是另一个模块（消息状态机），不在本期范围

### Architecture Role

**14a（后端）属于渐进式升级**：
- `core/qwen.py` 横向扩展（新增 `stream_chat`，旧 `chat` 保留）
- `services/rag/pipeline.py` 关键路径升级（`run` → `run_stream`，旧函数删除因为唯一调用方是 chat.py）
- `api/chat.py` 接口契约变化（response 从 JSON → SSE 文本流），但**入参不变**（前端零侵入）

**14b（前端）属于全新模块**：
- 不在 CLAUDE.md §6 规定的 `backend/app/` 分层里
- 内部按职责拆 5 个组件：`LoginForm / ChatPage / ConversationList / MessageList / MessageInput`
- 状态在 `ChatPage` 集中管理（`messages` / `streamingText` / `currentSessionId`），子组件纯展示
- API 封装在 `api.ts` 一个文件（不拆 `api/auth.ts` + `api/chat.ts` + `api/conversations.ts`），等真拆不动了再拆

**与后端的边界**：
- 前端只通过 HTTP API 通信，**不直接连 MySQL / Redis / Qdrant**
- SSE 协议是「后端约定 → 前端实现」单向，前端不需要后端改东西
- 错误处理两边独立：前端拦截 HTTP status + 解析 error 事件，后端 try/except 包住 generator

### 整体架构图（最终态）

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       Frontend (Vue3 + Vite)                            │
│                                                                         │
│   App.vue  ──► LoginForm / ChatPage                                     │
│                    │                                                    │
│                    ├──► ConversationList  (left sidebar)                │
│                    ├──► MessageList      (right chat) + ▊ cursor        │
│                    └──► MessageInput     (bottom textarea)              │
│                                                                         │
│   api.ts  ──► fetch /auth/*  (Cookie)                                   │
│           ──► fetch /conversations/*  (Cookie)                           │
│           ──► fetch /chat  (SSE stream, AsyncGenerator)                 │
│                                                                         │
│   Vite dev proxy  ──► :5173 → :8000 (同源)                              │
└─────────────────────────────────────┬───────────────────────────────────┘
                                      │  HTTP + SSE
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        Backend (FastAPI)                                │
│                                                                         │
│   api/                                                                  │
│   ├── /auth/*          ──► auth_service (bcrypt + JWT + Cookie)         │
│   ├── /admin/*         ──► require_admin ──► ingest/knowledge (Qdrant)  │
│   ├── /chat            ──► StreamingResponse ──► pipeline.run_stream    │
│   └── /conversations/* ──► 直接 ORM 查询 (cursor pagination)            │
│                                                                         │
│   services/                                                             │
│   ├── rag/pipeline.run_stream()   编排 + yield (meta, token, done)      │
│   ├── rag/ingest / knowledge      Qdrant + MySQL 元数据                 │
│   ├── chat_history                Redis 热 + MySQL 冷                   │
│   └── audit_service               best-effort operation_logs            │
│                                                                         │
│   core/                                                                 │
│   ├── embedding  (text-embedding-v3)                                    │
│   ├── qwen       (chat + stream_chat)                                   │
│   ├── security   (JWT)                                                  │
│   └── config     (env 统一读)                                            │
│                                                                         │
│   clients/                                                              │
│   ├── qdrant    (vectors)                                               │
│   ├── redis     (sessions)                                              │
│   └── mysql     (ORM)                                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 演进路径更新
- ✅ 后端 10 大模块 + Auth + Write-Through 闭环
- ✅ 会话：Redis 24h 热路径 + MySQL 永不过期冷路径
- ✅ 知识库：Qdrant 向量 + MySQL 元数据
- ✅ 审计：chat / ingest / delete_knowledge 全部上报
- ✅ 会话列表 API + 分页优化（§12 / §13）
- ✅ SSE 流式输出 + Vue3 前端（§14）— **系统从「后端 demo」升级为「可用聊天产品」**
- ✅ V1.2 收尾优化（§15）— 7 项小步快跑，零架构变更
- ⏳ Admin 知识库管理页前端（用同一套 Vue 组件 + 复用 /admin API）
- ⏳ Agent 工具调用（LangGraph，CLAUDE.md §2 当前禁用但后续模块可能开启）
- ⏳ 生产部署：nginx proxy_buffering off + HTTPS + 多 worker 部署

---

## 15. V1.2 收尾优化（Front-end Polish + Observability + Engineering Closure）

**阶段定位**：V1.1 已完成"代码层可长期维护 + 前端产品感"，V1.2 收尾 7 项小步快跑，全部在前一阶段基础上增量补齐，**零架构变更、零新依赖、零业务逻辑改动**。

> 关键约束（用户明令）：
> - ❌ LangGraph / Agent
> - ❌ 多租户
> - ❌ 重写 RAG
> - ❌ 拆服务架构
> - ❌ 改 MySQL schema

### 15.1 前端 UX 收口（3 项）

#### What
1. **MessageList 流式滚动节流** — `requestAnimationFrame` 包装 `scrollTop = scrollHeight`，避免每 50ms 触发 layout thrashing
2. **MarkdownView 代码块 + 复制按钮** — `<pre>` 角上加按钮，Clipboard API + execCommand 兜底，复制 raw text（无 v-html，Vue `{{ }}` 自动转义）
3. **MessageList skeleton 占位** — 接 `loading?: boolean` prop，3 条 shimmer 占位（CSS `@keyframes shimmer` 渐变扫光）

#### Why
- **滚动节流**：V1.1 实现的 SSE 流式在长文（200+ 字）时偶现掉帧，`watch + nextTick` 每 token 触发 1 次 scroll 引起 layout thrashing
- **代码块复制**：RAG 回答常含 SQL / Python 示例，复制是高频操作，缺按钮用户要手选文本
- **Skeleton**：切会话时从空 → 历史消息，中间 ~200ms 是空白，给用户"还在加载"的视觉反馈

#### Tech
- `requestAnimationFrame`（浏览器原生 60fps 节流，零依赖）
- `navigator.clipboard.writeText` + `document.execCommand('copy')` 兜底（旧浏览器 / 非 https）
- CSS `@keyframes shimmer`（`background: linear-gradient` + `background-size: 200% 100%`）

#### Flow
```
流式 token 涌入
  → watch([messages.length, streamingText]) 触发
  → nextTick
  → scrollToBottom()
     ↓ rAF 包裹（一帧内多次调用合并）
     → scrollEl.scrollTop = scrollEl.scrollHeight

assistant 文本含 ```code```
  → marked.lexer 切 token
  → template 渲染 <div class="code-block">
     <button class="copy-btn">复制</button>
     <pre><code>{{ raw text }}</code></pre>
  → 用户点按钮
     → navigator.clipboard.writeText
        成功 → 按钮显示"已复制" 1.5s
        失败 → 兜底 execCommand

切换会话
  → ChatPage: messagesLoading = true
  → MessageList 接 loading prop
  → 顶部渲染 3 条 .skeleton（w-60% / 80% / 45%）
  → loadMessages 完成
  → loading = false → 隐藏 skeleton
```

#### Problem → Fix
- **rAF 节流后部分场景漏滚**：某些边角场景下 rAF 被跳帧
  - 当前：只在 watch 触发时调用 rAF，不影响 messages.length 变化场景（nextTick 已保证 DOM 更新）
- **clipboard API 在非 https 下不可用**：本地 dev 用 `localhost`（浏览器视为 secure context，OK）；某些环境可能失败
  - 解决：try/catch 包住 + 兜底 `document.execCommand('copy')`（已 deprecated 但仍可用）

#### Role
V1.1 已经把前端从"能跑"升级到"产品感"，V1.2 继续在交互细节上打磨。3 项改动都在原有组件内增量，**不动 ChatPage 主架构**（状态仍集中在 ChatPage，子组件纯展示）。

---

### 15.2 后端轻量增强（2 项）

#### What
1. **`/health` 增强** — 检测 MySQL / Redis / Qdrant 三组件状态，每个独立 try/except，任一 down → 整体 `degraded`
2. **`/me` 增强** — 新增 `UserOutStats` schema，2 条独立 COUNT 查询，附带 `message_count` + `conversation_count`

#### Why
- **可观测性**：上线后排查故障第一步是看哪个组件挂了，原 `/health` 只回固定字段 `ok`，出问题不知道是哪
- **用户感知**：前端 header 可以显示"消息数 / 会话数"，给用户"我用了多少"的产品感（V2 再接 dashboard 也有数据基础）
- **降级而非失败**：单组件挂不应让 `/health` 整体 500（监控告警会被噪声淹没）

#### Tech
- SQLAlchemy 2.0 `func.count` + `select`（新式语法）
- Pydantic v2 `class UserOutStats(UserOut)` 继承 + 2 字段
- 同步 SQLAlchemy（FastAPI 自动放 default executor，**不**需要 async engine）

#### Flow
```
GET /health
  │
  ├─► MySQL: engine.connect() + SELECT 1
  │     ok → components.mysql.status = "ok"
  │     except → components.mysql = {status: "down", error: str(e)[:100]}
  │
  ├─► Redis: redis_get().ping()
  │     ok → components.redis.status = "ok"
  │     except → components.redis = {status: "down", error: ...}
  │
  ├─► Qdrant: qdrant_get().get_collections()
  │     ok → components.qdrant.status = "ok"
  │     except → components.qdrant = {status: "down", error: ...}
  │
  └─► overall = "ok" if all ok else "degraded"
     return {status, env, version, components}

GET /auth/me
  │
  ├─► get_current_user (depends 注入)
  ├─► COUNT messages WHERE user_id=? AND deleted=0
  ├─► COUNT conversations WHERE user_id=? AND deleted=0
  └─► UserOutStats(id, username, ..., message_count, conversation_count)
```

#### Problem → Fix
- **MySQL 同步调用阻塞 async 函数？**：`get_engine().connect()` + `execute` 是同步 SQLAlchemy，理论上阻塞 event loop
  - 分析：FastAPI 把 `def` 端点跑在 threadpool，`async def` 端点的同步调用也会放 default executor（async 0.115+ 行为）
  - 决策：保持同步（与项目其他 ORM 调用风格一致），如未来需要可改 `await asyncio.to_thread(...)`
- **COUNT 查询性能**：messages 表可能有几万行
  - 已有 `Message.user_id` 索引（§10 建的），单 COUNT < 10ms
  - 决策：单查两次而非 1 次 JOIN（语义清晰，user 想知道"我有多少消息"和"我有多少会话"是独立指标）
- **`/auth/me` response_model 改为 UserOutStats 兼容性**：`/auth/login` 仍返回 `LoginResponse{user: UserOut}`，没改；只有 `/me` 升级

#### Role
属于 `api/` 层（路由 + 参数解析 + 调 services）。**不**写业务逻辑（COUNT 是直接 ORM 查询，简单统计不抽 service）。为后续 dashboard / 用户中心页提供数据基础。

---

### 15.3 工程化收口（3 项）

#### What
1. **`.env.example` 补全 7 字段** — 之前只覆盖 `core/config.py` 的 60% 字段，缺 JWT / bcrypt / Cookie
2. **`docker-compose.prod.yml` 新建** — prod override 文件，与 dev compose 用 `-f` 合并，不复制 services 块
3. **README 重写** — 删所有"⏳待开发"项，加 prod 部署段、故障排查表、API 速查表

#### Why
- **`.env.example` 字段不同步**：新人 `cp .env.example .env.dev` 启动报 JWT_SECRET 占位符错，得翻 git history
- **prod/dev 共存**：dev 用 docker compose，prod 走 `docker-compose -f a.yml -f prod.yml` 合并，配置源唯一（dev 是主）
- **README 老旧**：V1.0 时写的"⏳待开发"全是 V1.1/V1.2 已完成的；接手人第一眼看到"待开发"会误判项目完成度

#### Tech
- Docker Compose `extends` / `-f` 合并（**没用 extends 因为 frontend 服务在 dev 中被注释**，合并更通用）
- Markdown 表格（故障排查速查）
- `comm -23` diff `config.py` 字段 vs `.env.example` 字段验证一致性

#### Flow
```
# 启动 prod
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
               --env-file .env.prod up -d --build

# docker-compose.yml 中 services.api 是基础
# docker-compose.prod.yml 中 services.api 是覆盖
# 合并结果（docker compose config 验证）：
#   api.environment.COOKIE_SECURE = "true"  ← 来自 prod
#   api.environment.APP_ENV = "prod"        ← 来自 prod
#   api.environment.DATABASE_URL = ...      ← 来自 base
#   api.deploy.resources.limits.memory = "2147483648"  ← 来自 prod（覆盖 base 的 1G）

# frontend 服务在 dev 中被注释
# prod 中显式声明 → 合并后启用
```

#### Problem → Fix
- **prod 网络与 dev 冲突**：两套 compose 用同名网络 `customer-service-backend` 会冲突
  - 解决：prod override 改 `networks.backend.name = customer-service-backend-prod`
- **`nginx:alpine` 镜像源限流（V1.0 已知）**：dev 中 frontend 被注释，prod 启用后仍可能受限
  - 解决：保留注释说明"如镜像源恢复即可启用"，prod compose 启用并加 `restart: always`
- **.env.example 字段验证**：用 grep 提取两边字段，comm -23 找差异
  - 验证结果：3 个 `*_URL`（DATABASE_URL / QDRANT_URL / REDIS_URL）未列在 .env.example，是**故意**的（compose 内置 service-name DNS，外部无意义）

#### Role
属于"工程化收口"，不在 `app/` 分层里。是项目可交付性的最后 1 公里——文档 / 配置 / 部署流程对齐。

---

### 演进路径更新
- ✅ V1.0：14 业务模块 + RAG + MySQL schema + Auth
- ✅ V1.1：with_safe_session / Config 集中化 / chat_history 拆 3 文件 / Markdown + 流式光标 + 空态
- ✅ V1.2：/health 组件状态 / /me 用户统计 / 流式滚动节流 / 代码块复制 / skeleton / prod compose / .env 补全
- ⏳ V2.0：Agent（用户当前禁用）

### V1.2 设计原则

| 原则 | 体现 |
|------|------|
| **零架构变更** | 7 项改动全部在原文件内增量，不动 RAG / API 契约 / MySQL schema |
| **零新依赖** | 不引代码高亮库（Prism +150KB）、不引 monitoring SDK（Prometheus）、不引动画库 |
| **用户视角优先** | 选"能少加依赖就少加"——复制按钮手写而非引 clipboard polyfill、滚动节流用 rAF 而非引 lodash |
| **失败可降级** | `/health` 任一组件 down 不让整体 500；clipboard API 失败有 execCommand 兜底；rAF 节流不影响核心功能 |
| **配置源唯一** | prod compose 用 `-f` 合并而非复制粘贴；.env.example 与 config.py 字段对齐验证 |

### 关键 curl 验证记录

| 测试 | 结果 |
|------|------|
| `curl /health` 正常 | `{status: "ok", components: {mysql/redis/qdrant: ok}}` |
| `docker stop redis` + `curl /health` | `{status: "degraded", components.redis: {status: "down", error: "Error -3..."}}` |
| `docker start redis` + `curl /health` | `{status: "ok", ...}` |
| `curl /auth/me` (admin cookie) | `{id: 1, ..., message_count: 14, conversation_count: 7}` |
| `docker compose -f a.yml -f prod.yml config` | 解析无错，frontend 服务启用，API 内存 2G |
| `npm run build` | 通过（CSS 8.72→9.71 KB, JS 111.80→112.72 KB，骨架 +1KB）|

### 反思

- **重写诱惑**：V1.2 期间多次想"既然改 /me 加 stats 不如顺便加 Redis 缓存"、"既然改 /health 不如顺便加 Prometheus metrics"——克制住了，按"只做当前任务"原则保持小步快跑
- **测试覆盖缺口**：V1.2 没有写单测，全靠 curl 验证。后期如果项目要长期维护，pytest 覆盖应该补（V1.3 候选）
- **可观测性瓶颈**：当前 /health 是同步阻塞调用（3 个组件 ~10-30ms），高频监控（1Hz）有压力，但当前用户量级不是瓶颈
- **前端构建产物体积**：marked +1KB CSS +1KB JS，总 ~113KB gzipped 41KB，仍在可接受范围（< 60KB 警戒线）
- ⏳ 可观测性：结构化日志 + Prometheus metrics + 链路追踪

---

## 15.4 V1.2 后续：全 Docker 化部署 + nginx 配置踩坑

**触发场景**：V1.2 完成后，用户要求"全栈 Docker 模式"（之前是 backend 在 Docker、frontend 用 `npm run dev` 在 Windows 宿主机）。于是启用了 `deploy/docker-compose.yml` 里被注释的 frontend 服务，**第一次把 Vue3 + nginx + API 真正串成生产链路**，暴露了多个 V1.0 写 placeholder nginx 时未触发的坑。

> 这一节是"从能跑 → 上线"的最后一公里，比 V1.2 主任务更费时，是项目中值得分享的运维/配置经验。

### What
1. **启用 frontend 容器**：取消 `docker-compose.yml` 中 frontend 注释 + mount `../frontend/dist` 替代占位 index.html
2. **重写 nginx.conf**（占位版本只服务静态 HTML，未考虑 API 反代 + SSE）
3. **修 3 个 healthcheck**：Qdrant 用错路径 + 缺工具、Frontend `localhost` 解析 IPv6
4. **修 1 个 CORS 跨源 301**（见下文 Problem → Fix）

### Why
- 用户决策 "B. 全 Docker 模式"：演示 / 交付场景需要"与生产 100% 一致"
- 占位 nginx 是 V1.0 临时方案（"前端待开发"），不反代 API、不处理 SSE、不考虑 location 尾斜杠语义
- Healthcheck 在 V1.0 写 compose 时**只在 dev compose 默认行为下测过**（ip 都能通），切到全 Docker 后 IPv6/工具缺失的差异才暴露

### Tech
- **nginx 1.27（alpine 镜像自带）** + Vue3 dist bind mount
- **bash `/dev/tcp`**（仅 bash 支持，sh 不行，必须显式 `bash -c`）
- **nginx `$http_host` vs `$host`**：前者保留端口，后者剥端口
- **nginx location 尾斜杠语义**：`/path/` 强制带尾斜杠（不匹配 `/path` 请求，触发自动 301）；`/path` 兼容两种

### Flow（全链路）
```
浏览器 http://localhost:5173
  │
  ▼ nginx :80
  │ location /            → SPA 静态（try_files ... /index.html）
  │ location /auth        → proxy_pass http://api:8000
  │ location /conversations → proxy_pass http://api:8000
  │ location /admin       → proxy_pass http://api:8000
  │ location /chat        → proxy_pass http://api:8000
  │   + proxy_http_version 1.1
  │   + proxy_buffering off    ← SSE 关键
  │   + proxy_read_timeout 300s
  │
  ▼ FastAPI :8000（API 容器）
  │ /auth/* /conversations/* /admin/* /chat
  │
  ▼ MySQL/Redis/Qdrant
```

### Problem → Fix（本节重点）

#### Problem 1：Qdrant healthcheck 用错路径 + 缺工具

- **症状**：`docker ps` 显示 qdrant `(unhealthy)`，但 `curl /healthz` 200
- **根因**：V1.0 写 compose 时用了 `curl -fsS http://localhost:6333/health`，但：
  - Qdrant 1.10 健康端点是 `/healthz`（带 z），不是 `/health`
  - Qdrant 镜像无 `curl`（Rust 极简镜像）
- **Fix**：
  ```yaml
  # /health → /healthz
  # curl → bash + /dev/tcp
  test: ["CMD-SHELL", "bash -c 'exec 3<>/dev/tcp/127.0.0.1/6333 && printf \"GET /healthz HTTP/1.0\\r\\n\\r\\n\" >&3 && grep -q 200 <&3'"]
  ```
- **教训**：写 healthcheck 时**先在容器内 `docker exec sh -c "which curl wget nc"`** 确认工具存在；`/dev/tcp` 是 bash 特有，sh 不支持，必须显式 `bash -c`

#### Problem 2：Frontend healthcheck `localhost` 解析 IPv6 失败

- **症状**：`unhealthy`，`wget: can't connect to remote host`
- **根因**：Alpine 容器里 `getent hosts localhost` 返回 `::1`（IPv6），但 nginx 只 listen IPv4 `0.0.0.0:80`
- **Fix**：`http://localhost/` → `http://127.0.0.1/`
- **教训**：Alpine 的 `localhost` 默认优先 IPv6，**容器内健康检查一律用 `127.0.0.1`**（或显式 `ip -4`）

#### Problem 3：CORS 跨源 301（最隐蔽，浏览器才暴露）

- **症状**：浏览器 console 报 CORS 错误：
  ```
  Access to fetch at 'http://localhost/conversations/' (redirected from 'http://localhost:5173/conversations')
  from origin 'http://localhost:5173' has been blocked by CORS policy
  ```
- **根因链**：
  1. 浏览器 → `GET http://localhost:5173/conversations`（无尾斜杠）
  2. nginx `location /conversations/`（带尾斜杠）**前缀不匹配** `/conversations`（缺尾斜杠）
  3. nginx 触发"自动 301 规范化"：`Location: http://localhost/conversations/`
  4. **`$host` 变量把端口剥了**：`Host: localhost:5173` 在 nginx 里 `$host` = `localhost`（无端口）
  5. 浏览器跟随 301 到 `http://localhost/conversations/`（默认端口 80，**跨源**）
  6. CORS 拦截 + 失败
- **Fix 1**：location 改为不带尾斜杠（兼容 `/conversations` 和 `/conversations/X`）
  ```nginx
  # 改前：location /conversations/ { ... }   # 强制带尾斜杠
  # 改后：location /conversations { ... }    # 兼容两种
  ```
- **Fix 2**：`$host` → `$http_host`（保留端口）
  ```nginx
  proxy_set_header Host $http_host;   # 保留 localhost:5173
  # proxy_set_header Host $host;      # 变成 localhost（剥端口）
  ```
- **教训**：
  1. **nginx location 尾斜杠语义**：`/path/` 是"严格匹配以 /path/ 开头"（会触发自动 301 规范化）；`/path` 是"宽松前缀匹配"（兼容 `/path` 和 `/path/X`）
  2. **`$host` vs `$http_host`**：nginx 内部变量，`$host` 来自请求行第一个 `.` 之前（**剥端口**），`$http_host` 来自 HTTP 头（**保留端口**）
  3. **生产 nginx 反代必须 `$http_host`**：否则后端 redirect 的 Location 会丢端口，触发跨源

### 反思（运维视角）

- **占位配置的债务**：V1.0 写 nginx 占位时只服务静态 HTML，所有"反代 API、SSE、跨域、location 语义"全没暴露。这次切到全 Docker 才暴雷。**经验：基础设施代码也要"按未来真实形态写"，否则后期改动是 1+ 倍成本**
- **"本地 dev 通 ≠ 容器 prod 通"**：
  - Vite dev proxy 是 Node 写的，自动处理尾斜杠、Host、Cookie
  - nginx 不会"贴心"帮你，location/Host 配置错了就直接 301/404/CORS
  - **解法：production-like 环境尽早启**（V1.2 后期才切全 Docker，本可以更早）
- **CORS / 301 / 307 三角关系**：
  - 浏览器跨源请求**自动跟随重定向**（CORS spec），但跟随后的跨源请求**重新走 CORS 协商**
  - 301（Moved Permanently）浏览器会缓存 → 一旦配错，所有用户都受影响
  - 307（Temporary Redirect）不缓存，但每次都触发
  - **最佳实践：API 路径不带尾斜杠**（V1.0 `/conversations` 已经这样），nginx location 也不带，前后端都一致
- **容器 healthcheck 工具矩阵**（V1.2 期间整理）：

  | 镜像 | 缺什么 | 推荐探活方式 |
  |------|--------|--------------|
  | nginx:alpine | 无 IPv6 `localhost` | `wget http://127.0.0.1/` |
  | qdrant | 无 curl/wget/nc/python | `bash -c '/dev/tcp/ip/port + 简单 HTTP'` |
  | redis:alpine | 无 wget（早期） | `redis-cli ping`（自带）|
  | mysql:8.0 | 无 curl | `mysqladmin ping`（自带）|
  | python:slim | 大多有 curl | `curl -fsS` |

- **测试覆盖缺口**：这次 CORS 301 在 V1.2 期间完全没发现，**直到用户用浏览器实际点开页面才暴露**。教训：**配置类改动必须端到端浏览器验证**（curl 走 8000 直连是测不出来的，curl 走 5173 反代才会暴露 301 行为）

### 关键 nginx.conf 范本（可直接复用）

```nginx
server {
    listen 80;
    charset utf-8;
    client_max_body_size 20M;

    # SPA 静态
    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }

    # API 反代（注意：location 不带尾斜杠，Host 用 $http_host 保留端口）
    location /auth          { proxy_pass http://api:8000; proxy_set_header Host $http_host; }
    location /conversations { proxy_pass http://api:8000; proxy_set_header Host $http_host; }
    location /admin         { proxy_pass http://api:8000; proxy_set_header Host $http_host; }

    # SSE 流式（必须禁 buffer + HTTP/1.1 + 长超时）
    location /chat {
        proxy_pass http://api:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 300s;
        proxy_set_header Host $http_host;
    }
}
```

**4 个易错点全避免**：location 尾斜杠、`$host` 剥端口、SSE buffer、auth 头透传。

---

## 16. M1 数据层：电商 Schema 升级 + Mock 种子（V2 升级第一站）

**文件**：
- 新增 `backend/app/models/product.py`、`backend/app/models/order.py`、`backend/app/models/refund.py`
- 改 `backend/app/models/__init__.py`、`deploy/mysql/init/01_schema.sql`
- 新增 `scripts/seed_ecommerce_data.py`

### What
为 V2 电商化升级建数据基座。3 张新表 + 1 张子表 + 完整状态机 + 5 订单 mock 数据：

| 表 | 行数 | 关键字段 |
|---|---|---|
| products | 10 | sku / name / price / attributes(JSON) / review_text / stock |
| orders | 5 | order_no / status(状态机) / total_amount / user_id |
| order_items | 6-7 | order_id / product_id / sku(冗余) / product_name(冗余) / qty / unit_price / subtotal |
| refunds | 1 | refund_no / order_id / reason / status / amount |

### Why
- V1 5 张表（users / conversations / messages / knowledge_documents / operation_logs）全是「基础设施表」，缺业务实体
- V2 核心能力（商品 / 订单 / 退款）必须有结构化数据支撑，否则 RAG + Tool 都没东西可调用
- 状态机用 Python Enum 约束，DB 存 VARCHAR(16)：避免 ENUM 类型难迁移（schema 演进痛点）
- 冗余 `sku` + `product_name` 在 order_items：商品改名/下架后，订单历史仍可读（电商核心需求）

### Tech
- **SQLAlchemy 2.0**：`Mapped[T] + mapped_column()` 类型化声明，与 §10 风格一致
- **状态机强约束**：业务层必须用 `OrderStatus.PAID.value` 写，禁止字符串硬编码
- **JSON 字段**：`products.attributes`（动态属性：颜色/规格）、`products.review_text`（独立字段不入 RAG）
- **逻辑外键**：按 CLAUDE.md §9 约定，所有外键用 BigInt 存 id，不建 DB 级 FK 约束（保留 `KEY ... (user_id, ...)` 索引）
- **`Base.metadata.create_all` 幂等建表**：seed 脚本首调，已存在则跳过，演示阶段绕开手工 SQL 迁移

### Flow（数据生命周期）

```
                          seed_ecommerce_data.py
                                  │
   ┌──────────────────────────────┼──────────────────────────────┐
   ▼                              ▼                              ▼
┌────────────────┐    ┌────────────────┐            ┌────────────────┐
│ DELETE 清空    │    │ Base.create_all│            │ INSERT mock    │
│ (子表→父表)    │    │ (幂等建表)      │            │ (10/5/6/1 行)  │
└────────────────┘    └────────────────┘            └────────────────┘
                                                         │
                                                         ▼
                                                ┌────────────────┐
                                                │ status 分布验证 │
                                                │ pending/paid/  │
                                                │ shipped/deliv/ │
                                                │ refunded  1:1  │
                                                └────────────────┘
```

### Problem → Fix

#### Problem 1：`relationship()` 要求 FK（最隐蔽）
- **症状**：`sqlalchemy.exc.NoForeignKeysError: Could not determine join condition between parent/child tables on relationship Order.items`
- **根因**：CLAUDE.md §9 禁 DB 级 FK，但 SQLAlchemy `relationship()` 需要 FK 才能推导 join 条件
- **取舍**：删 `Order.items` 和 `OrderItem.order` 两个 relationship，靠 `order_id` 字段手动 JOIN
  - 选了「不写 relationship」而非「加 ForeignKey 但不创建 DB 约束」——后者需要 `ForeignKey(...) + use_alter=True` 等魔法，可读性差
  - 当前 5 订单数据量级，N+1 不是瓶颈；将来真要 ORM 联表再加 `primaryjoin` 表达式
- **教训**：CLAUDE.md 的「禁 DB 级 FK」和 SQLAlchemy ORM 的便利有冲突，**取舍要明确写注释**

#### Problem 2：Windows 主机解不到 Docker 容器名 `mysql`
- **症状**：`socket.gaierror: [Errno 11001] getaddrinfo failed`，连接失败
- **根因**：Docker 内部 DNS 只在容器网络生效，Windows 宿主机没装 docker-compose 的 service discovery
- **Fix**：seed 脚本跑前 override `DATABASE_URL` 用 `127.0.0.1:3307`（端口映射，host→container）
- **教训**：写进 feedback memory（`feedback_docker_mysql_localhost`），下次直接用

#### Problem 3：表不存在导致 seed 失败
- **症状**：第一次跑 seed 直接 `Table 'customer_service.refunds' doesn't exist`
- **根因**：MySQL init 目录只在「数据卷为空」时跑一次，本项目 schema 是 V1.0 建的，新增表不会自动建
- **Fix**：seed 脚本首调 `Base.metadata.create_all(bind=get_engine())` 幂等建表（已存在跳过）
- **设计取舍**：写 Alembic 迁移太重（M1 是 demo 阶段、可重建），create_all 够用

#### Problem 4：Python stdout 缓冲导致「看似无输出」
- **症状**：`python seed.py` 退出码 0 但 `> seed.log 2>&1` 空文件
- **根因**：Windows + Python 3.11 组合下，`logging.basicConfig` 默认行为有缓冲
- **Fix**：调试用 `python -u`（强制无缓冲）或直接 docker exec mysql 验证数据落地（最终方案）

#### Problem 5：docker exec 输出 `????` 中文乱码
- **症状**：`docker exec mysql mysql -e "SELECT ..."` 商品名显示 `????`
- **根因**：shell 默认 GBK 编码，MySQL 客户端不自动转 UTF-8
- **Fix**：用 Python 直读（`PYTHONIOENCODING=utf-8`），数据本身是 UTF-8 正确存储的

### Architecture Role
属于 `models/` 层（数据实体定义）+ `scripts/`（运维脚本）。被未来模块消费：
- **M2 Order Service**：用 `Order` / `OrderItem` 做订单查询（结构化，非 RAG）
- **M2 Refund Service**：用 `Refund` + 调 Policy RAG 做退款复合路径
- **M2 Product Service**：用 `Product` 做商品列表 + RAG 详情增强
- **M3 Intent Classifier**：规则关键词 + refund/order/product 四分类
- **M4 Response Synthesizer**：tool 结构化结果（订单状态、商品详情）作为高优先级事实

### 演进路径更新
- ✅ V1.0 ~ V1.2：14 业务模块 + 5 张基础设施表 + Auth + SSE + Vue3
- ✅ **M1**：电商 4 张表 + 状态机 + mock 数据（当前进度）
- ⏳ M2：Product / Order / Refund / Policy Service + Tools Layer
- ⏳ M3：Intent Classifier（规则 + LLM 兜底）
- ⏳ M4：Response Synthesizer 多源融合 + 端到端 SSE
- ⏳ M5：4 类意图验收 40 用例 + 浏览器联调
- ⏳ V3.0：Agent（CLAUDE.md §2 当前禁用）

### 关键 SQL 验证记录

```sql
SELECT COUNT(*) FROM products WHERE deleted=0;     -- 10 ✅
SELECT COUNT(*) FROM orders WHERE deleted=0;       -- 5 ✅
SELECT COUNT(*) FROM order_items WHERE deleted=0;  -- 7 ✅
SELECT COUNT(*) FROM refunds WHERE deleted=0;      -- 1 ✅

SELECT order_no, status, total_amount FROM orders ORDER BY create_time;
-- ORD20260601005 | refunded  | 698.00
-- ORD20260615004 | delivered | 4299.00
-- ORD20260622003 | shipped   | 1299.00
-- ORD20260621002 | paid      | 6898.00
-- ORD20260620001 | pending   | 899.00
-- 状态机 5 个状态全覆盖（缺 completed，V2 可补）
```

### 反思
- **YAGNI 验证**：`product_categories` 表本想做（类目树），评估后放弃——10 个商品用 `attributes.category` 字段足够，单独表 = 过度设计
- **「演示可重建」权衡**：`shipping_addresses` / `logistics` 表也延后，service 层 mock 返回假数据足够端到端跑通
- **relationship 取舍是当下最大设计决策**：删掉后失去 ORM 联表便利，但保留「禁 DB 级 FK」的项目一致性。**记下：M2 写 service 时如发现 N+1 严重，再加 `primaryjoin` 表达式回来**

---

## 17. M2 服务 + Tools 层（V2 第二站）

**文件**：
- 新增 `backend/app/tools/{__init__,order_tool,product_tool,refund_tool}.py`
- 新增 `backend/app/services/{order_service,refund_service,policy_service}.py`

### What
把 §16 的数据层暴露成业务能力。**两层结构**：
- **Tools 层（薄）**：纯 DB 查询 / mock，不调 LLM / 不做 RAG
- **Services 层（厚）**：编排 tool + 跨源融合（如 refund = tool + policy RAG）

### Why
- **CLAUDE.md §6 原则**：「tool 函数只做 DB 查询，不调 LLM 不做 RAG」——把"决策"和"取数"拆开
- **不引入 Agent 框架**：CLAUDE.md §2 禁 LangGraph，Tool 单步调用 + Service 编排足以覆盖电商客服 4 类意图
- **越权防护放 Tool 层**：每个 Tool 方法都强制收 `user_id`，DB 查询自带 `WHERE user_id=?`，上层不会忘记

### 接口矩阵

| 类 | 方法 | 数据源 | 关键防护 |
|---|---|---|---|
| OrderTool | list_user_orders / get_order_by_no | MySQL orders | `WHERE user_id=?` |
| OrderTool | get_order_items | MySQL order_items | order_id 已校验 |
| OrderTool | get_logistics | mock | status→物流状态映射（5 状态全覆盖）|
| ProductTool | get_by_sku / list_products / search_by_keyword | MySQL products | `WHERE status=1 AND deleted=0` |
| RefundTool | list_user_refunds / get_refund_by_no | MySQL refunds + orders | `WHERE user_id=?` |
| RefundTool | check_refundable | MySQL + 规则判断 | 7 天无理由规则封装 |
| PolicyService | search_policy | Qdrant knowledge_base | V2.5 简化为全量搜，无 doc_type 过滤 |
| OrderService | list_user_orders / get_order_detail | 编排 OrderTool | 复合 order+items+logistics |
| RefundService | check_refundable_with_policy | 编排 RefundTool + PolicyService | 双源融合，synthesizable 标记 |

### Flow（refund 复合路径 — V2 最有代表性的服务）

```
RefundService.check_refundable_with_policy(user_id, order_no, query)
  │
  ├─► RefundTool.check_refundable(user_id, order_no)
  │     └─► 7 天无理由规则 + status 状态机 → {refundable, reason, days_since_order}
  │
  └─► PolicyService.search_policy(query, top_k=3)
        └─► embed_text(query) → Qdrant top-3 → [{text, source, score}]
  
  return {
    tool_result: {refundable, reason, ...},
    policy_docs: [{text, source, score}, ...],
    synthesizable: 至少一边有结果
  }
```

### Tech
- **同 Session 复用**：所有 Tool 方法用 `with with_safe_session(commit=False) as db`，读路径用 readonly session
- **N+1 防护**：RefundTool.list_user_refunds 批量预取 order_no_map（1 次 orders 查询，避免 N 次）
- **状态机映射**：OrderTool.get_logistics 用 dict 硬编码 6 种 status → (物流状态, 位置)
- **policy 软过滤**：V2.5 因 KB 67 条全是政策、KB 没 doc_type 字段，**简化**为「全量搜 + 不后过滤」；等 V2.6 引入商品 KB 时再加 `doc_type=policy` 过滤

### Problem → Fix

#### Problem 1：QDRANT_URL 容器名错（最坑的运维 bug）
- **症状**：PolicyService.search_policy 调 `httpx` POST 收到 **502 Bad Gateway**，但 `curl POST http://localhost:6333/...` 正常 400
- **根因链**：
  1. `deploy/docker-compose.yml:30` 写的是 `QDRANT_URL=http://qdrant:6333`（V0 早期占位名）
  2. 实际容器名是 `customer-service-qdrant`（项目改名后未同步）
  3. 容器内 POST 出去 → DNS 找不到 `qdrant` → 网关返 502
  4. host 上 curl 走 `localhost:6333` 是端口映射，绕过了 DNS，所以"通"
- **Fix**：`deploy/docker-compose.yml:30` 改 `QDRANT_URL=http://customer-service-qdrant:6333`，重建 API 镜像
- **教训**：写 docker-compose 必须用**实际 container_name**，不用裸 service name；占位名长期不动 = 隐藏 bug

#### Problem 2：policy_service collection 名错
- **症状**：修完 #1 后，policy_service 改报 `Collection knowledge_base not found`
- **根因**：policy_service.py 写死 `COLLECTION_NAME = "customer_service_kb"`（V0 占位），实际 Qdrant 只有 `knowledge_base`
- **Fix**：改 `COLLECTION_NAME = "knowledge_base"`，删 `doc_type` 后过滤（KB 67 条全政策）
- **教训**：collection 名是**数据契约**，改名前必须 grep 全仓库

#### Problem 3：容器镜像未重建导致新文件不见
- **症状**：修完配置后 `docker exec ls /app/app/services/policy_service.py` 报 No such file
- **根因**：docker-compose `up -d` 默认**不重新 build**，只 restart 容器
- **Fix**：`docker compose build api` 先重建镜像，再 `up -d`
- **教训**：修改 Dockerfile COPY 的目录（`app/`）必须 `build`；只改 env 不需要 build

#### Problem 4：MySQL container 名 mismatch（沿用 M1 教训）
- 与 M1 Problem 2 同根因，复用 `feedback_docker_mysql_localhost` memory
- seed 脚本 / smoke test 都用 `127.0.0.1:3307`，**第一次写对就再没踩**

### Architecture Role

```
api/chat.py (M4 才有)
  │
  ▼
services/synthesizer.py (M4)
  │
  ├──► OrderService ─────► OrderTool ─────► MySQL orders
  ├──► RefundService ──┬──► RefundTool ────► MySQL refunds + orders
  │                    └──► PolicyService ──► Qdrant knowledge_base
  ├──► PolicyService ─────► Qdrant knowledge_base (复用)
  └──► (M4 没用 ProductService，只用 ProductTool)
              │
              ▼
       ProductTool ─────► MySQL products
```

属于 `services/` + `tools/` 双层架构（CLAUDE.md §6 分层扩展）：
- **Tools 层（新增）**：业务能力的"原子操作"，封装 DB 查询 + 越权防护
- **Services 层（现有）**：业务能力的"编排"，可组合 tool + RAG + LLM

被未来模块消费：
- **M3 Intent Classifier**：用 Tool 类名作为方法分派 hint（不直接调，但 entity 抽取后 service 层调）
- **M4 Response Synthesizer**：4 路径分发都调 services 层
- **M5 端到端测试**：每个 Tool 方法都跑过 smoke test

### 演进路径更新
- ✅ V1.0 ~ V1.2 + 全 Docker 部署 + M1 数据层
- ✅ **M2**：3 Tools + 3 Services + 全量 smoke test 通过
- ✅ **M3**：Intent Classifier + 独立 /intent 端点 + 10 用例 100%
- ✅ **M4**：Response Synthesizer + 集成到 /chat + 10 用例 100%
- ⏳ M5：浏览器联调（前端适配 V2 多源融合答案）
- ⏳ V2.6-A/B/C：状态记忆 / Tool-first / 语义意图实验
- ⏳ V3.0：Agent（CLAUDE.md §2 当前禁用）

### 关键 smoke test 验证记录

| Tool/Service | 验证场景 | 结果 |
|---|---|---|
| OrderTool.list_user_orders(user_id=1) | 5 笔订单按时间倒序 | ✅ |
| OrderTool.get_logistics("ORD20260622003") | shipped→运输中/深圳转运中心 | ✅ |
| ProductTool.get_by_sku("SKU001") | ZP1 ¥5999 SKU001 | ✅ |
| ProductTool.search_by_keyword("耳机") | BP1 ¥899 | ✅ |
| RefundTool.check_refundable(delivered 10d) | 不可退 + 7 天超期 | ✅ |
| RefundTool.check_refundable(shipped) | 可退 + shipped | ✅ |
| RefundTool.check_refundable(refunded) | 不可退 + 已退款 | ✅ |
| PolicyService.search_policy("退货政策") | top-1: policy_return_main 0.738 | ✅ |
| PolicyService.search_policy("退款多久到账") | top-1: policy_return_faq_03 0.883 | ✅ |

### 反思

- **Layer 拆分真的有用**：Tool 写完立刻能 smoke test（不依赖任何 service / LLM），开发速度比"service 内联 tool"快一倍以上
- **mock 数据是负债也是资产**：物流 mock 当前足够 demo，但生产必须接真实快递 API（顺丰/菜鸟），记到 V3+ 路线图
- **Service 编排复杂度待观察**：refund_service 同时调 2 个 tool + 1 个 RAG，单元测试将是挑战（需要 mock Qdrant / MySQL），M5 时补 pytest fixture
- **配置 bug 是 devops 不是代码 bug**：M2 大半时间花在 Qdrant / collection / container name 上，**纯代码量只占 30%**——这条经验要写进 CLAUDE.md §7「环境问题速查」

---

## 18. M3 Intent Classifier（V2 第三站）

**文件**：
- 新增 `backend/app/schemas/intent.py`、`backend/app/services/intent_service.py`、`backend/app/api/intent.py`
- 改 `backend/app/main.py`（挂 router）

### What
独立意图分类服务。4 类意图 + 3 级 fallback + 实体抽取：
- **4 类意图**：`order_query` / `refund_query` / `product_query` / `policy_query`
- **3 级 fallback**：规则（关键词+正则）→ LLM 兜底 → 默认 policy_query
- **实体抽取**：自动识别 query 里的 `order_no`（ORD123）/ `sku`（ZP1/BP1/LP1）

### Why
- V1.2 时代所有 query 走统一 RAG pipeline，**无视意图差异**——商品咨询和物流查询用同一份 KB 召回，效率差
- V2 架构（PROJECT_DESIGN.md §3）核心是「按意图分派」：order/refund 走 tool，product/policy 走 RAG
- 规则优先避免 LLM 调用开销：**M3 验收 §9 要求 < 100ms**（规则命中时几乎无开销）

### 接口

```
POST /intent/classify
  请求：{"query": "...", "last_intent": "可选（V2.6 启用）"}
  响应：{"intent": "order_query", "confidence": 1.0, "method": "rule",
         "entities": {"order_no": "ORD001", "sku": null, "keywords": []}}
```

### Flow

```
IntentService.classify(query, last_intent=None)
  │
  ├─► rule_classify(query)
  │     │
  │     INTENT_RULES = [
  │       ("refund_query",  [...r"我[要想要]?退款", r"能退吗", ...]),
  │       ("policy_query",  [...r"7\s*天无理由", r"包邮", ...]),
  │       ("order_query",   [...r"我的订单", r"物流", ...]),
  │       ("product_query", [...r"多少钱", r"ZP\d", ...]),
  │     ]
  │     return {intent, confidence=1.0, method="rule"}  # 命中即返
  │
  ├─► llm_classify(query)  # 规则未命中
  │     │
  │     prompt = "你是电商客服意图分类器... 输出 JSON"
  │     qwen_chat(temperature=0.1, max_tokens=80)
  │     parse → {intent, confidence=0.0~1.0, method="llm"}
  │
  ├─► default {intent: "policy_query", confidence: 0.5, method: "default"}
  │
  └─► _extract_entities(query)
        ORDER_NO_RE = \bORD\d{3,}\b
        SKU_RE      = \b(?:ZP|BP|LP)\d{1,3}\b
```

### Tech
- **规则顺序敏感**：refund 在前（语义最明确）→ policy（兜底）→ order → product，最后 product 默认包含 SKU 前缀
- **few-shot prompt**：LLM 兜底时给 3 个示例（product / policy / order），提升分类准确率
- **temperature=0.1**：分类任务要确定性，避免 LLM "创意发挥"
- **JSON 强校验**：LLM 输出可能包 ``` 或解释文字，用 `re.search(r"\{[^{}]+\}", reply)` 提取首个 JSON 段
- **正则大小写不敏感**：`re.IGNORECASE` 让用户写 "ord001" 也能抽到

### 验证（10 用例 = 100%）

| # | query | 期望 | 实测 | method | 延迟 |
|---|-------|------|------|--------|------|
| 1 | 我想退款 | refund_query | ✅ | rule | 14ms |
| 2 | 已经签收 5 天了还能退货吗 | refund_query | ✅ | rule | 4ms |
| 3 | 我的订单到哪了 | order_query | ✅ | rule | 3ms |
| 4 | ORD123 发货了吗 | order_query | ✅ | rule + entity=ORD123 | 3ms |
| 5 | 快递派送中吗 | order_query | ✅ | rule | 3ms |
| 6 | ZP1 现在多少钱 | product_query | ✅ | rule + entity=ZP1 | 3ms |
| 7 | BP1 的续航怎么样 | product_query | ✅ | rule + entity=BP1 | 4ms |
| 8 | 你们这有没有手机 | product_query | ✅ | **llm** (0.90) | 2836ms |
| 9 | 保修期多久 | policy_query | ✅ | **llm** (0.95) | 959ms |
| 10 | 双十一有什么活动 | policy_query | ✅ | **llm** (0.95) | 1855ms |

**规则命中 7 条 (3-27ms)，LLM 兜底 3 条 (1-3s)，全部正确**。验收标准 §8 ≥ 80% → 实际 100%。

### Problem → Fix

#### Problem 1：意图分类边界 — "退款"规则把 policy 误命中
- **症状**：测试 "7 天无理由退货运费谁出" → refund_query（错）
- **根因**：原 refund 规则含 `r"7\s*天无理由"`，命中"7 天无理由退货"
- **设计取舍**：「7 天无理由退货运费谁出」是政策咨询，「我的订单 ORD123 7 天内能退吗」才是退款申请
- **Fix**：refund 规则要求**明确个人语境**（"我要/想" + 退），把"7 天无理由"挪到 policy_query 规则
- **反思**：规则冲突是**规则系统的本质难题**；V2.6-C 计划用 embedding 语义分类做 A/B 实验

#### Problem 2：「ZP1 保修多久」被 r"保修"误命中 policy
- **症状**：含 sku 的商品保修咨询 → policy_query（应该 product_query 优先）
- **根因**：policy_query 规则含 `r"保修"`，匹配顺序在 product_query 前
- **Fix**：从 policy_query 删 `r"保修" / r"质保"`，让纯"保修期多久"靠 LLM 兜底到 policy_query
- **教训**：**规则顺序 + 规则内容**都要测试覆盖；10 用例发现 2 个边界 bug 算正常

#### Problem 3：intent_service 没接进 /chat 的边界
- **方案选择**：用户选"独立 /intent 端点"，不接 /chat（M3 不动现有 RAG 路径）
- **为什么对**：M4 整合时一次性接入，避免 M3 改动冲击 V1.2 在线服务
- **CLAUDE.md §5 Scope Lock** 体现：M3 只动 intent 三件套 + main.py 1 行 include_router

### Architecture Role

属于 `services/intent_service.py`（编排）+ `schemas/intent.py`（契约）+ `api/intent.py`（HTTP）。
- 不在 CLAUDE.md §6 原始分层里（**新增 intent 子模块**），但符合"业务编排层"定位
- 复用 `core/qwen.chat()` 做 LLM 兜底（与 RAG pipeline 同源）
- 复用 `core/embedding.embed_text()`？**没有**——V2.5 阶段规则+LLM 够用，V2.6-C 才用 embedding 做语义分类

被 M4 Synthesizer 消费：
```python
Synthesizer.run_stream(query, user_id, history)
  ├─► IntentService.classify(query) → {intent, entities, method, confidence}
  └─► if intent == "order_query": _handle_order(...)
       elif intent == "refund_query": _handle_refund(...)
       elif intent == "product_query": _handle_product(...)
       else: _handle_policy(...)
```

### 反思

- **规则 vs LLM 兜底是经典 tradeoff**：规则可控、可解释、快，但覆盖边界难写；LLM 通用但慢、要 API key
- **3 级 fallback 设计合理**：rule → llm → default 既保性能（rule 命中时 < 100ms）又保覆盖（rule 漏了 LLM 兜底，再漏 default 不空响应）
- **M3 故意独立端点是关键决策**：先验证分类准确率（100%），再让 M4 整合，避免 M4 调试时分不清"是分类错还是合成错"
- **未来 V2.6-C 升级方向**：用 embedding 相似度替代 keyword 规则，可解决"边界冲突"问题；embedding 召回的"相似意图列表" + 阈值判断，理论上比正则准确率高

---

## 19. M4 Response Synthesizer + /chat 集成（V2 第四站）

**文件**：
- 新增 `backend/app/services/synthesizer.py`
- 改 `backend/app/api/chat.py`（1 行替换 + meta 透传）
- 新增 `deploy/tests/test_chat_e2e.py`

### What
**多源融合层** — 把 M2 service + M3 intent + V1.2 pipeline 整合成一个统一的 `/chat` 流式输出：
1. **意图分类**（M3）：决定走哪条路径
2. **按意图分派**：order/refund 走 tool，product 走 tool，policy 走 RAG
3. **多源融合 prompt**：tool 数据 + policy RAG + history，按 §7 硬约束排序（tool > policy > product > history）
4. **单 LLM 流式输出**：qwen stream_chat，SSE token/done
5. **fallback 兜底**：分派异常 → V1.2 RAG pipeline（不破坏线上）

### Why
- V1.2 时代所有 query 走 `pipeline.run_stream(query, top_k=5)` — **无差别 RAG**，3-4s 内 80% 答案质量差
- V2 架构的核心是「**正确的 query 走正确的路径**」：order 查 tool（毫秒级）、refund 查 tool+policy（融合）、product 查 DB、政策查 RAG
- **单 LLM 原则**：所有路径最终都调 1 次 qwen stream_chat，prompt 不同但 LLM 不变；避免「每模块各自调 LLM」的成本 + 不一致风险
- **fallback 兜底是新代码保护机制**：M4 是大改，V1.2 用户不能受影响；任何分派异常 → V1.2 RAG，用户体验不变

### 架构位置（V2 最终态）

```
POST /chat (SSE)
  │
  ├─► load_history_with_fallback (V1.2 保留)
  │
  ├─► Synthesizer.run_stream(query, user_id, history)  ← M4 新增
  │     │
  │     ├─► IntentService.classify(query)            [M3]
  │     │
  │     ├─► 分派（4 路径 + 1 兜底）：
  │     │     ├─ order_query   → OrderService + tool_block
  │     │     ├─ refund_query  → RefundService + tool_block + policy_block
  │     │     ├─ product_query → ProductTool + product_block + policy_block
  │     │     ├─ policy_query  → PolicyService + policy_block
  │     │     └─ 异常/兜底    → V1.2 rag_run_stream (不变)
  │     │
  │     ├─► _build_chat_prompt（§7 硬约束：tool > policy > product > history）
  │     │
  │     └─► qwen stream_chat → SSE (meta, token, done)
  │
  ├─► write-through Redis + MySQL + audit (V1.2 §11 保留)
  │
  └─► SSE response (text/event-stream)
```

### 关键代码片段

```python
# synthesizer.py 核心分发
class Synthesizer:
    @staticmethod
    def run_stream(query, user_id, history):
        intent_result = IntentService.classify(query)
        try:
            if intent_result["intent"] == "order_query":
                yield from Synthesizer._handle_order(query, user_id, intent_result)
            elif intent_result["intent"] == "refund_query":
                yield from Synthesizer._handle_refund(query, user_id, intent_result)
            elif intent_result["intent"] == "product_query":
                yield from Synthesizer._handle_product(query, intent_result, history)
            else:
                yield from Synthesizer._handle_policy(query, intent_result, history)
        except Exception as e:
            # 兜底 → V1.2 RAG
            logger.exception(f"synth.dispatch 异常 fallback 到 V1.2 RAG: {e}")
            for event_type, data in v12_rag_run_stream(query, 5, history):
                yield (event_type, data)

# §7 prompt 硬约束（顺序固定）
def _build_chat_prompt(*, intent, tool_block, policy_block, product_block, history_block, query):
    sections = []
    if tool_block:    sections.append(f"【事实陈述】(最高优先级)\n{tool_block}")
    if policy_block:  sections.append(f"【政策依据】\n{policy_block}")
    if product_block: sections.append(f"【商品知识】\n{product_block}")
    if history_block: sections.append(f"【对话历史】\n{history_block}")
    sections.append(f"问题：{query}")
    return "\n\n".join(sections)
```

### 验证（M4 端到端 10/10 = 100%）

| # | 意图 | query | 关键证据（节选） | 延迟 |
|---|------|-------|------------------|------|
| 1 | order_query | ORD20260622003 现在到哪了 | "运输中/深圳转运中心/SF20260622003" | 2.6s |
| 2 | order_query | 我的订单有哪些 | 5 笔订单含 ORD 号/金额/状态 | 4.5s |
| 3 | order_query | ORD20260615004 物流 | "已签收/北京海淀/SF20260615004" | 1.4s |
| 4 | order_query | ORD20260620001 啥情况 | "待发货/BP1 ¥899" | 2.7s |
| 5 | refund_query | ORD20260622003 能退吗 | "可退 + shipped 已签收 4 天 + 7 天政策条款" | 4.7s |
| 6 | refund_query | ORD20260615004 还能退吗 | "不可退 + 已签收 10 天超 7 天 + 换货建议" | 3.3s |
| 7 | product_query | ZP1 现在多少钱 | "¥5999.0 / SKU001" | 2.0s |
| 8 | product_query | 你们这有什么耳机 | "BP1 ¥899 + 保修政策" | 5.5s |
| 9 | product_query | ZP1 保修多久 | "1 年主机/6 月电池 + 范围说明" | 3.2s |
| 10 | policy_query | 7 天无理由退货运费谁出 | "卖家/买家分别承担 + 首重 12 元" | 4.0s |

**未登录 order_query 单独验证**：返回"请登录"模板，不报 500。

### Problem → Fix（沿路修的 7 个 bug）

| # | bug | 根因 | 修在哪 |
|---|-----|------|--------|
| 1 | Qdrant POST 502 | QDRANT_URL 容器名错 | deploy/docker-compose.yml:30 |
| 2 | policy_service 找不到 collection | collection 名错 + 无 doc_type 过滤 | services/policy_service.py |
| 3 | OrderService.list_user_orders() got unexpected keyword argument 'limit' | M2 服务签名不含 limit | services/synthesizer.py 调用点 |
| 4 | "退货运费谁出"被 refund 规则误命中 | refund 含"7 天无理由" | services/intent_service.py 拆出 policy_query |
| 5 | "ZP1 保修多久"被 r"保修" 误命中 policy | policy_query 规则在 product_query 前 | services/intent_service.py 删 r"保修" |
| 6 | product_query 整句搜不到商品 | search_by_keyword 对长 query 噪音词干扰 | services/synthesizer.py 加 _search_by_keyword_window |
| 7 | chat.py meta 丢 intent 字段 | V1.2 chat.py 只透传 contexts/scores | api/chat.py **meta |

### 关键技术点

#### 4.1 SKU 实体 vs MySQL SKU 不匹配
- **现象**：M3 抽到 `sku=ZP1`，但 MySQL.products.sku = `SKU001`（不是 `ZP1`）
- **决策**：商品查询走 ProductTool.search_by_keyword（名字 LIKE "ZP1" 命中 SKU001），不走精确 sku 查询
- **代码**：
  ```python
  if sku:
      exact = ProductTool.get_by_sku(sku)
      if exact:
          products = [exact]
      else:
          products = ProductTool.search_by_keyword(sku, limit=5)  # ZP1 → SKU001
  ```
- **未来**：V2.6-B 商品 ingest 进 KB 后，sku 实体可走 RAG（语义召回），不依赖 keyword

#### 4.2 滑动窗口抽 query 实词（product_query 兜底）
- **现象**：「你们这有什么耳机」整句搜 → 空 list（"你们"等噪音词干扰）
- **解决**：滑动窗口抽 2-3 字实词（"耳机"）→ 命中 BP1
- **代码**：
  ```python
  def _search_by_keyword_window(query, limit=5):
      candidates = []
      for size in (2, 3):
          for i in range(len(query) - size + 1):
              c = query[i:i + size]
              if re.fullmatch(r"[\u4e00-\u9fff]+", c) and c not in seen:
                  candidates.append(c)
      for kw in reversed(candidates):  # 倒序查，尾巴词优先
          ps = ProductTool.search_by_keyword(kw, limit=limit)
          if ps: return ps
      return []
  ```
- **反思**：当前不引 jieba 等分词库（避免依赖膨胀）；2-3 字滑窗对商品类目够用

#### 4.3 meta 事件协议扩展（向后兼容）
- V1.2 chat.py 只透传 `contexts` / `scores` 字段；M4 引入 `intent` / `entities` / `tool_result_preview`
- **修法**：chat.py 改为 `{**data}` 全量透传，保证 V1.2 字段不变（write-through MySQL 还用 `contexts`），同时新字段对前端可见
- **前端兼容性**：未来需要的话可在 Vue3 api.ts 加 `MetaEvent.intent` 等 typed 字段（V2.6+ 前端适配时做）

#### 4.4 fallback 兜底的事务边界
- **设计**：分派 try/except 包住，**只 catch Exception**，ValueError 不 catch（query 为空应该让上层 500）
- **不破坏 V1.2**：fallback 路径走 `v12_rag_run_stream`，元事件格式与 V1.2 完全一致（contexts + scores）
- **意义**：M4 上线初期如果某个 Tool 出问题，用户感知是"答案质量略降"而不是"全挂"

### Architecture Role

属于 `services/synthesizer.py`（V2 核心编排层）：
- **CLAUDE.md §6 扩展**：在 `services/` 下新增 synthesizer（与 rag/pipeline 平级）
- **不复用 V1.2 pipeline**：保留 `rag/pipeline.py` 作为 fallback，独立 synthesizer 走"分类 + 分派"
- **跨模块集成**：同时调 IntentService (M3) + 各 Service (M2) + qwen stream_chat (V1.2)

被消费：当前只被 `api/chat.py` 1 个调用方；未来可被：
- **V2.6-A 状态记忆**：在 synthesizer 入口加 last_intent 参数，state 注入 prompt
- **V2.6-B Tool-first**：强化 prompt 硬约束（已部分实现）
- **V3.0 Agent**：synthesizer 可被 Agent tool 调用作为子任务

### 反思

- **M2 → M4 跨 3 个模块，调试总时长 1.5h**：7 个 bug 里 4 个是配置/集成层（QDRANT_URL / collection 名 / OrderService 签名 / meta 透传），3 个是规则边界（refund vs policy / product_query keyword）。**说明 V2 升级最大的成本在"老模块给新模块让路"的胶水代码**
- **fallback 兜底是 MVP 必备**：M4 第一版没加 fallback，结果 #2 直接"建议登录 App 查看"——Tool 没数据 → LLM 瞎答。fallback 后用户体验至少不崩
- **Prompt 硬约束的威力**：把"tool 数据必须优先"写进模板后，LLM 不再忽略结构化数据；之前测试发现 V1.2 时代 LLM 会完全无视 KB 召回（基于低分 context 瞎编），V2 用 tool 数据 + policy RAG 双源后答案明显稳定
- **测试期望 vs 答案质量的鸿沟**：本次 e2e 测试 #4 #8 期望"含 pending / BP1"等关键词，但 LLM 答得"中文润色版"反而把这些词替换成"待发货 / ¥899"——**测试断言要更灵活**（断言语义而非字面）
- **CLAUDE.md「最小修改原则」在 M4 反例**：要接 4 个 service + 改 chat.py + 加 synthesizer.py，单模块"最小"是不可能的；**正确理解是"不动无关代码"**——M4 没碰 RAG pipeline、没碰 MySQL schema、没碰 frontend

### 演进路径更新
- ✅ V1.0 ~ V1.2 + 全 Docker 部署
- ✅ M1 数据层 + M2 服务层 + M3 路由层 + M4 融合层
- ⏳ M5：浏览器联调（前端需适配 V2 多源答案 + intent 字段透传）
- ⏳ V2.6-A 状态记忆：Redis session add current_state JSON
- ⏳ V2.6-B Tool-first：synthesizer prompt 强化
- ⏳ V2.6-C 语义意图：embedding classifier A/B 实验
- ⏳ V3.0：Agent 工具链 + 商品 RAG ingest + 多轮指代

---

---

## 9. M5 端到端验收 + 修复模块

**文件**：`deploy/tests/test_m5_e2e.py`（新建）、`backend/app/services/synthesizer.py`（改）、`backend/app/services/intent_service.py`（改）

### What
实现 M5 端到端验收：4 类意图 × 10 用例 = 40 条，通过率门槛 ≥ 85%。首轮实测 77.5%，诊断失败根因后修复 3 处代码 + 1 处测试预期，最终 3 次平均 95.8% 稳过。

### Why
- PROJECT_DESIGN.md §8 验收标准是项目可演示/可讲解的硬指标
- M5 通过意味着「用户问 → 意图路由 → 数据召回 → LLM 合成 → 答案」全链路在 4 类典型场景下都跑得通
- 验收过程暴露 3 个真实 bug，正好是 V2.x 上线前必须修的

### Tech Stack
- **SSE 客户端**：urllib.request + 手动解析 `data:` 行（同 test_chat_e2e.py）
- **JWT 复用**：admin JWT（user_id=1 名下 5 单全在），覆盖登录态所有用例
- **判定标准**：每条同时满足 SSE 完整 + 意图正确 + 关键词命中 + latency < 5s
- **结果统计**：分意图通过率 + 总通过率 + 失败用例汇总（CI 可直接 sys.exit）

### Flow
```
test_m5_e2e.py main()
  ↓
遍历 40 CASES（query, login, expect_intent, must_contain_any, note）
  ↓
每条：call_chat(query, jwt) → /chat SSE → 收集 meta/token/done
  ↓
problems = []
  ├─ SSE 异常 → +
  ├─ intent 不符 → +
  ├─ 关键词全部缺失 → +
  └─ elapsed > 5s → +
  ↓
按意图分桶统计 → 输出表格 + 失败明细
  ↓
sys.exit(0 if pass_rate >= 0.85 else 1)
```

### 修复记录（首轮 77.5% → 最终 95.8%）

#### 失败 9 条的归因（首轮诊断）

| # | 问句 | 我第一诊断 | 实际根因 |
|---|------|-----------|---------|
| 5 | "ORD20260601005 退款进度" | 规则冲突 ❌ | **测试预期写错**：应预期 refund_query |
| 18 | "退款的钱退到哪里" | 规则冲突 | LLM 兜底分类错（未登录 + 无规则匹配）|
| 22 | "BP1 续航怎么样" | KB 缺失 ❌ | **路径问题**：`_handle_product` 只查 MySQL 不查 Qdrant，KB 里的 specs 从未读出 |
| 25 | "SKU002 的配置" | KB 缺失 ❌ | 同上：MySQL 没 specs，KB 也没被读 |
| 26,32,39 | 笔记本/保修/优惠券 | latency 超 | LLM 长答案无约束 |
| 34 | "什么时候发货" | 规则冲突 | 「发货」抢匹配 → order_query |
| 40 | "电池保修多久" | 规则冲突 | 「电池」抢匹配 → product_query |

#### 实际修复（最小修改）

**修 1（测试）**：`test_m5_e2e.py` #5 预期改 refund_query。**+1 条**。

**修 2（`_handle_product` 加 KB RAG）**：
- 原代码：`PolicyService.search_policy("保修政策", top_k=2)` — 用固定串只能召回保修
- 改成：`PolicyService.search_policy(query, top_k=3)` + KB 结果合并进 product_block
- 解决 #22 #25：续航/配置/电池容量等 specs 现在能从 KB 召回
- 副作用：删除了冗余的 policy_block（product_query 不需要 policy 块）

**修 3（Intent 规则加 policy 优先项）**：
- policy_query 块新增 5 条：`什么时候发货 / 多久发货 / 发货时间 / 电池.*保修 / 保修多久 / 质保多久`
- 顺序：policy_query 块在 order_query / product_query 之前 → 优先匹配
- 解决 #34 #40

**修 4（Synthesizer prompt 长度约束）**：
- `SYSTEM_PROMPT_BASE` 加 `"回答控制在 200 字以内，不要长篇大论，先给结论再补充细节"`
- 解决 #26 #32 #39：LLM 输出从 500-800 字降到 200-300 字
- 副作用：所有路径（chat/order/refund/product/policy）受益

#### 未做的（Dense 通道）

`Intent Classifier 加 Dense 通道` 的原计划被规则补丁替代：
- #34 #40 用精确规则覆盖（收益 80%，复杂度 0）
- #18 是 LLM 兜底边角案例（短句+无登录+无明显关键词），Dense 也难救，留 V3

#### 最终成绩

| 指标 | 首轮 | 修复后 |
|------|------|--------|
| 总通过率 | 31/40 = 77.5% | **38.3/40 = 95.8%（3 次平均）** |
| order_query | 9/10 = 90% | 9/9* = 100% |
| refund_query | 9/10 = 90% | 10/11* = 90.9% |
| product_query | 7/10 = 70% | **10/10 = 100%** |
| policy_query | 6/10 = 60% | **10/10 = 100%** |
| 最大 latency | 8269ms（#39）| 4043ms（#38）|

*#5 从 order_query 改到 refund_query，分母调整

### Problem → Fix
- **诊断先于动手**：第一轮我把 9 条失败都归因为「规则顺序」，实际只有 4 条是；剩下 5 条分别是测试预期错（1）/ 路径 bug（2）/ LLM 长答案（3）。盲目改规则顺序是错误路线。
- **数据驱动查根因**：直接打 Qdrant vs /chat 端点对比，发现 BP1 chunk 在 Qdrant 里有但 /chat 返回 sources=0 — 立刻定位到 `_handle_product` 不读 KB
- **LLM 非确定性**：3 次跑波动 ±1 条，验收门槛设 85% 是合理的（防单次运气），目标 ≥ 95% 才有信心上线
- **重启容器**：Dockerfile `COPY app/` 是 bake-in，改代码必须 `docker compose build api` + `up -d api`（约 30s）

### Architecture Role
- **test_m5_e2e.py**：M5 验收交付物，CI 可直接 `python deploy/tests/test_m5_e2e.py`，exit code 表验收结果
- **3 处代码修复**：每处都是「最小修改 + 单一职责」：
  - synthesizer.py `_handle_product` 加 KB RAG → 解决 KB specs 召回
  - intent_service.py 加 5 条 policy 规则 → 解决规则顺序盲区
  - synthesizer.py `SYSTEM_PROMPT_BASE` 加长度约束 → 解决 LLM 长答案
- **未做的 Dense 通道**：保留为 V3 任务，PROJECT_DESIGN §3 已写「禁 Rerank / Dense 评估在 V3」

---

## 10. P1 性能压测 + A+B 修复模块

**文件**：`deploy/tests/test_load_50users.py`（新建）、`deploy/tests/test_load_sweep.py`（新建）、`backend/app/core/qwen.py`（改）、`backend/app/services/synthesizer.py`（改）

### What
对 /chat 端点做 50 并发用户压测，发现 100% 错误率（429 RateLimitError）。诊断根因为「无并发控制 + 无 429 重试」，实施 A+B 最小修复：
- **A**：synthesizer 加 `threading.Semaphore(10)` 限流 LLM 并发
- **B**：qwen.chat / qwen.stream_chat 加 RateLimitError 指数退避重试（1s/2s/4s）

修复后再次压测，错误率从 100% → 0%，但并发承载上限受上游 DashScope 限制。

### Why
- PROJECT_DESIGN §9 spec 写「并发 > 50 + P95 < 5s」，但只测过单用户
- 单用户 M5 通过 ≠ 系统能撑 50 并发 — 必须真压测
- 发现 50 并发下 100% 失败（429）后立即暴露架构 gap

### Tech Stack
- **压测工具**：Python `concurrent.futures.ThreadPoolExecutor`（Windows 兼容，无 wrk/ab 依赖）
- **HTTP**：`requests` + `stream=True` + `iter_lines` 解 SSE
- **限流**：`threading.Semaphore(10)`（10 路并发调 LLM，超出排队）
- **重试**：指数退避 `wait = base * 2^attempt`，最大 3 次
- **流式重试边界**：仅 retry 连接阶段（`create()`），不 retry 已开始的流（已 yield chunk 不可回退）

### Flow

#### 压测执行流
```
warmup (3 个 GET /chat 让 LLM/Qdrant 缓存热起来)
  ↓
ThreadPoolExecutor(n_users) 起 n 个线程
  ↓
每线程：one_query(query) → POST /chat (stream=True)
  ↓
解析 SSE：meta / token / done / error
  ↓
汇总 first_token_ms / total_ms / error
  ↓
P50/P95/P99 统计 + §9 对比
```

#### A+B 修复架构
```
请求进入 /chat
  ↓
Synthesizer.run_stream → _stream_llm
  ↓
with _LLM_SEMAPHORE (10):     ← A：限流
  ↓
  for chunk in qwen.stream_chat():
      ↓
      内部 retry loop (3 次指数退避)   ← B：429 重试
      ↓
      chunk yield
  ↓
yield ("done", ...)
```

### Problem → Fix
- **测试设计先于结论**：原 50 users × 5 queries = 250 同时请求的负载模型过激。改 50 users × 1 query = 50 同时请求才符合 §9 spec 「并发 > 50」的字面含义
- **错误类型是诊断信号**：从 429 → ReadTimeout 的变化说明限流被解决，剩下的是队列等待 + 客户端超时问题
- **客户端 timeout 干扰诊断**：默认 30s 不足以看真实 P95，加到 60s 才能区分「系统慢」vs「客户端主动断」

### Sweep 结果（5/10/20/30/50 并发，2026-06-27 实测）

| 并发 | P95 总耗时 | 错误率 | §9 达标 |
|------|-----------|--------|---------|
| 5 | 3377ms | 0% | ✅ |
| 10 | 8989ms | 0% | ❌ |
| 20 | 46303ms | 0% | ❌ |
| 30 | 28435ms | 0% | ❌ |
| 50 | 65567ms | 0% | ❌ |

**结论**：
- ✅ A+B 修复彻底消除 429 错误（错误率全 0%）
- ❌ §9 「> 50 并发 + P95 < 5s」在当前 DashScope 公共 tier 下**不可同时满足**
- 📊 实测最大稳定并发（满足 §9 spec）= **5**

### 提升路径（未做）
- 升级 DashScope 到付费 tier（更高 QPM + 更高并发配额）
- 加 LLM 结果缓存（相同 query 直接复用上次 answer）
- 加 prompt 压缩（缩短 input → 减 token → 减响应时间）
- 异步多 LLM provider 路由（不同 model 分流）

### Architecture Role
- `test_load_50users.py`：固定 50 并发的标准压测脚本（验收 §9 字面项）
- `test_load_sweep.py`：扫 5/10/20/30/50 并发的甜点定位脚本（验收 §9 实际能力）
- A+B 代码修复：跨 core（qwen）+ services（synthesizer）两个模块，按 CLAUDE.md §6 仍是「core 给 services 用接口」，未破坏分层
