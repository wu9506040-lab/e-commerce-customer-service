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

---

## 20. V3 LangGraph 引入：退款流程重构 + 分层架构演进（2026-06-27）

### What
把退款流程从 V2.x 的 `RefundService.check_refundable_with_policy`（固定 3 步 if/else）升级为 LangGraph StateGraph，并引入「固定 vs 复杂」分层原则：
- **固定路径**（11 个模块：商品咨询 / 政策问答 / 订单查询 / 物流 / FAQ / Auth / Session 等）继续走 RAG + Tool
- **复杂路径**（步骤 ≥ 4 或需要条件分支或需要升级人工）走 LangGraph

交付物：
- `backend/app/services/refund_graph.py`（~210 行）：LangGraph 版退款图（6 Node + 3 条件边）
- `backend/tests/test_refund_graph.py`（~180 行）：16 个 LangGraph 单测
- `backend/tests/test_synthesizer_refund.py`（~190 行）：9 个 synthesizer 集成测试
- `backend/app/services/synthesizer.py`：新增 `_handle_refund_v3`，V2 改名 `_handle_refund_v2`，env 控制 dispatch
- `backend/app/core/config.py`：加 `USE_LANGGRAPH_REFUND: bool = False`
- `docs/refund_graph_v3.png`（26 KB）：状态图 PNG

### Why
**为什么引入 LangGraph（不是"规则禁了所以引入"）**：
1. **业务真到了复杂门槛**：退款流程从 3 步扩展到 5-6 步（查订单→判断→查政策→验凭证→升级人工），4 条条件分支 + 1 个升级路径，if/else 嵌套难维护
2. **可观测性**：LangGraph 的 stream + 状态可视化（mermaid PNG）+ Checkpoint 让每步可追溯，if/else 版只能 log
3. **市场匹配**：LangGraph 是当前招聘市场的常见技能要求之一，README 提到 LangGraph 便于搜索匹配
4. **生态复用**：LangGraph 的 StateGraph / Conditional Edge / Checkpoint / interrupt 这些能力自研做起来很重

**为什么不重构整个项目（YAGNI 原则）**：
- 11 个固定模块路径明确、调用链简单，LangGraph 引入是浪费
- 业务复杂度没到框架门槛时，框架就是负担
- **判定标准**：步骤 ≥ 4 或需要条件分支或需要自我修正 → LangGraph；其他继续 RAG + Tool

### Tech Stack
- **langgraph 0.2.76**：StateGraph + Conditional Edge + stream_mode="updates"
- **pydantic 2.13.4**：TypedDict 定义 state schema（`total=False` 容错）
- **现有项目组件**：OrderTool / PolicyService / qwen_chat（零新增基础设施）
- **依赖冲突**：pydantic-settings 2.3.4 vs 2.4.0（langchain-community 要求），暂不升级

### Flow（LangGraph refund_graph 6 Node）

```
输入: {user_id, order_no, query, user_proof}
   ↓
[Node 1] fetch_order
   调 OrderTool.get_order_by_no(user_id, order_no)
   算 days_since_order = (now - create_time).days
   return {order_info, days_since_order}
   ↓
[Node 2] judge_basic_refundable
   if not order → refundable=False, "订单不存在"
   elif status == "refunded" → refundable=False, "已退款"
   elif status == "delivered" and days <= 7 → refundable=True, "符合 7 天无理由"
   else → refundable=False, "超过 7 天或未签收"
   return {refundable, reason, days_since_order(pass-through)}
   ↓
[Conditional 1] should_fetch_policy(refundable)?
   - True → fetch_policy
   - False → synthesize（跳过查政策，节省 RAG 开销）
   ↓
[Node 3] fetch_policy（条件性，仅 refundable=True 执行）
   调 PolicyService.search_policy(query, top_k=3)
   return {policy_docs}
   ↓
[Conditional 2] should_check_proof(refundable)?
   - True → check_proof
   - False → synthesize
   ↓
[Node 4] check_user_proof（条件性）
   if "质量" in query and not user_proof → escalate_to_human=True, "需提供质量问题凭证"
   return {escalate_to_human}
   ↓
[Conditional 3] should_escalate(escalate_to_human)?
   - True → escalate
   - False → synthesize
   ↓
[Node 5] escalate_to_human（不走 LLM）
   return {final_answer: "您的情况需要人工客服..."}
   ↓ OR ↓
[Node 6] synthesize_answer（走 LLM）
   拼 prompt（4 段：事实>政策>商品>历史）+ 调 qwen_chat(temperature=0.3)
   return {final_answer: llm_reply}
```

**4 条路径覆盖**：
1. **可退 + 正常问题** → fetch_order→judge→fetch_policy→check_proof→synthesize（5 Node + 1 LLM）
2. **可退 + 质量问题无凭证** → fetch_order→judge→fetch_policy→check_proof→**escalate**（5 Node，0 LLM）
3. **不可退（超过 7 天/已退款）** → fetch_order→judge→synthesize（3 Node，跳过 policy/proof）
4. **订单不存在** → fetch_order→judge→synthesize（3 Node，judge 返回"订单不存在"）

### Problem → Fix

#### Problem 1：LangGraph stream_mode="updates" 是 per-node delta（最隐蔽）
**症状**：测试 `meta["days_since_order"] == 3` 失败，实际是 None。
**根因**：`refund_graph_app.stream(input, stream_mode="updates")` 每个 event 返回 `{node_name: node_return_value}`，**只含该 node 显式 return 的字段**。`fetch_order` 写了 `days_since_order` 到 state，但 `judge` 没显式 return 它，所以 `judge` 的 update event 里没有。
**Fix**：`judge_basic_refundable` 在每个分支多 return `days_since_order`（pass-through）。
**教训**：LangGraph stream update 跟 React state setter 一样是 partial delta，不是完整 state。要么 pass-through 字段，要么改用 `stream_mode="values"` 拿完整 state（但失去 delta 清晰性）。

#### Problem 2：LangGraph stream event 包含哨兵节点
**症状**：处理 `node_name == "__end__"` 时 KeyError。
**根因**：LangGraph stream 在每个 super-step 起点 emit `{"__start__": ...}`，终点 emit `{"__end__": ...}`。
**Fix**：循环里 `if node_name.startswith("__"): continue` 跳过哨兵。

#### Problem 3：LangGraph 内部抛异常会中断整个 SSE 流
**症状**：LangGraph 版的 `fetch_order` 如果 OrderTool 抛异常（比如 DB down），整个 refund_query 流程挂。
**根因**：LangGraph 的 `.stream()` 会传播内部异常到调用方，没有内置 fallback。
**Fix**：Synthesizer._handle_refund_v3 加 `try/except`，catch 后 `yield from _handle_refund_v2(...)` fallback 到 V2.x。**保险丝设计**：LangGraph 任何异常 → V2.x 接管 → 不影响线上。

#### Problem 4：pydantic-settings 版本冲突
**症状**：`pip install langgraph` 时提示 `langchain-community requires pydantic-settings>=2.4.0, but you have 2.3.4`。
**根因**：langchain-community 0.3.0 升级了 pydantic-settings 要求（2.4.0+）。
**Fix**：暂不升级（影响范围未评估，怕破坏现有 services）。LangGraph 0.2.76 跟 pydantic-settings 2.3.4 兼容，仅记录，V4 评估升级。

#### Problem 5：synthesizer.py 的 import 顺序混乱
**症状**：refactor 后 import 部分按字母序排，但 config 应该排在 core 模块最先。
**Fix**：按 `core/ → services/ → tools/ → models/` 顺序分组，让 import 顺序反映模块依赖层级。

### Architecture Role
- **LangGraph 子图**：`refund_graph_app` 是 V3 引入的第一个 LangGraph StateGraph，作为"复杂路径"的代表
- **Synthesizer 分派**：`run_stream` 在 refund_query 分支加环境变量 dispatch，是"框架切换"的接入口
- **Fallback 保险丝**：`_handle_refund_v3` 内部 try/except 兜底到 `_handle_refund_v2`，是 V3 上线的安全网
- **零侵入**：不动 IntentClassifier / OrderTool / PolicyService / qwen_chat，只在 synthesizer 层加分支
- **测试分层**：单测 16 个（LangGraph 本身）+ 集成测试 9 个（Synthesizer+LangGraph+SSE 协议+Fallback）= 25 个全过

### 演进路径更新
- **V3 现状**：1 个 LangGraph 图（refund）+ 25 测试全过 + 状态图 PNG 可视化
- **V3.1 计划**：智能导购图（多轮澄清 → 筛选 → 比价 → 推荐），复用相同 StateGraph 模式
- **V4 计划**：人在回路（interrupt_before）+ Checkpoint（SqliteSaver）+ 跨服务编排（订单+支付+物流子图组合）

### 反思
**1. 「为什么不用 LangGraph」是错的命题**
我之前回答"CLAUDE.md 禁用了所以不做"被指出来没有技术判断力。正确答案是：**评估过两个方案，业务复杂度到门槛才引入**。LangGraph 不是银弹，11 个固定模块继续 RAG+Tool 是对的；退款流程到 5-6 步 + 条件分支 + 升级路径，LangGraph 是对的。**框架成本要 < 自己写的成本才值得引入**。

**2. 「fallback 设计」是渐进式升级的核心**
V3 不破坏 V2.x：环境变量默认 false，V2.x 继续工作；验证 OK 后切 true；LangGraph 任何异常 → fallback 到 V2。这种"双轨并行 + 灰度切换 + 异常兜底"是复杂系统演进的标准模式，比"一次性重构"安全 10 倍。

**3. 「stream_mode=updates vs values」是 LangGraph 关键设计选择**
- values：完整 state，简单但失去 delta 清晰性，调试时不知道哪个 node 改了什么
- updates：每步 delta，清晰但要注意 pass-through，否则下游看不到前置字段
我选 updates（更能体现"Node 各自负责"），代价是要在 judge 里显式 pass-through days_since_order。**trade-off：清晰性 vs 字段冗余**。

**4. 「单测 vs 集成测试」分层覆盖**
- 单测（16 个）：只测 LangGraph 图本身 + Node 函数逻辑，mock 外部依赖（OrderTool/PolicyService/qwen_chat）
- 集成测试（9 个）：测 Synthesizer + LangGraph 协同 + SSE 协议 + Fallback
两层覆盖：LangGraph 改动不会破坏 Synthesizer，Synthesizer 改动不会破坏 LangGraph。**测试金字塔的实践：底层快、上层慢，分层 mock**。

**5. 「市场需求 vs 项目需要」的权衡**
市场需求 LangGraph 是事实。**项目需求和对外表达是两回事**：项目内部该用什么用什么（11 个固定 + 1 个 LangGraph），对外要会讲（V3 LangGraph 实战）。**不要为了表面潮流硬塞框架（11 个固定模块不该用 LangGraph），也不要忽视市场需求**。

---

## 21. M6 RAG 检索质量评估：hit@K 指标 + 合成评估集（2026-06-27）

### What
补齐 RAG 模块的**量化评估能力**——生成合成评估集（201 条 query），用 hit@1/3/5/10 指标量化当前 Qdrant 检索质量，并按 source 分组定位召回薄弱类目。

交付物：
- `scripts/gen_eval_set.py`（~145 行）：从 Qdrant scroll 读 67 条 doc，逐条用 Qwen 生成 3 个用户口吻 query
- `scripts/eval_hitk.py`（~200 行）：逐条 embed + Qdrant top-10 检索，计算 hit@K + 失败案例采样
- `data/eval_set_v1.json`（33 KB / 201 条）：合成评估集
- `data/eval_hitk_report.json`（178 KB）：详细结果 + 失败案例

### Why
**为什么需要 hit@K 评估**：
1. **没有量化指标 = "RAG 效果好不好"全凭感觉**。项目最怕「RAG 已实现」但说不出 hit@5 多少
2. **合成数据可行**：67 条 KB 文档都是自己写的结构化政策/FAQ，让 Qwen 围绕文档生成 query 质量可控（实测：201 条 0 失败）
3. **定位问题**：按 `source` 分组看 hit@1，找出"哪类目召回差"（如 `product_sku001` hit@1=0.0 → 后续优化可定向加 dense vector / 改 chunk 切分）

### Tech Stack
- **Qwen plus（temperature=0.8）**：query 生成
- **text-embedding-v3（1024 维）**：query embedding
- **Qdrant 1.10.1（Cosine distance）**：top-10 检索
- **Python statistics**（标准库）：p50/p90 时延统计
- **hit@K 指标**：binary relevance（每条 query 单一相关 doc）

### Flow
```
gen_eval_set.py：
  scroll Qdrant (limit=100) → 67 docs
  ↓
  for each doc:
    qwen chat(generate 3 queries)  →  JSON parse
  ↓
  save data/eval_set_v1.json (201 条)

eval_hitk.py：
  load eval_set (201 条)
  ↓
  for each query:
    embed_text(query)  → 1024 维向量
    qdrant.search(top_k=10)  → List[{id, score, payload}]
    check if relevant_doc_id in top-K
  ↓
  summarize: hit@1/3/5/10 + latency p50/p90 + by_source + miss samples
  ↓
  save data/eval_hitk_report.json + print report
```

### 评估结果（baseline）

| 指标      | 数值   | 解读                             |
|-----------|--------|----------------------------------|
| hit@1     | 0.517  | 约一半 query 第一条就召回对       |
| hit@3     | 0.721  | 前 3 条 72% 命中                 |
| hit@5     | 0.796  | 前 5 条 80% 命中（context window 友好）|
| hit@10    | 0.900  | 前 10 条 90% 命中                |
| 完全 miss | 20 / 201（10%）| 召回失败 query     |
| p50 时延  | 207 ms | 单条 query 检索时延              |
| p90 时延  | 328 ms | —                                |

**按 source 分组的关键观察**（hit@1 = 0 的需要重点优化）：
- `product_sku001` (0.0/0.0)：商品详情类，需更细粒度切分
- `admin_test` (0.0/0.0)：测试数据，预期会差
- `policy_promotion_preorder` (0.0/0.333)：促销规则类
- `product_sku003` (0.0/0.333)：商品详情类

### Problem & Fix

**问题 1：生成阶段 JSON 解析可能失败**
- 现象：Qwen 偶尔返回 ```json ... ``` 包裹或带解释文字
- 修复：prompt 强调"只输出 JSON"，解析时 regex 去掉 ``` 包裹；失败时 retry 1 次
- 实际：201 条 0 失败（命中率 100%）

**问题 2：eval 跑完不知道"哪类目差"**
- 修复：按 `source` 分组计算 hit@1/hit@5，sort by count desc，第一眼看到的是"召回最差的 SKU"

**问题 3：hit@K 单一指标不够**
- 当前是 binary relevance（单一相关 doc），未来可升级为 graded relevance（top-3 都有分）
- 后续：可加 MRR（Mean Reciprocal Rank）指标，看"第一条命中的排名质量"

### Role
**M6 = RAG 模块的"质检层"**。前面 M1-M5 实现"能不能跑"，M6 回答"跑得好不好"。这是把 RAG 从「demo」变成「产品」的必经一步——也是使用者必问的："你的 RAG 召回率多少？怎么测的？"

**项目摘要**：
> "基于 67 条电商知识库构建 201 条合成评估集，实现 hit@1=0.52 / hit@5=0.80 / hit@10=0.90 的检索质量，按 source 分组定位召回薄弱类目"

---

## 22. 知识库 V1.2 全场景补全（2026-06-27）

### What
针对系统能展示但 KB 未覆盖的功能，补全 **6 类 / 17 条**数据：
- 发票政策（3 条）：电子普票 / 专票申请 + 修改 / 丢失补开
- 支付问题（3 条）：6 种支付方式 + 付款未到账 / 换支付方式
- 账户安全（2 条）：密码 / 实名 / 换绑手机号
- 升级人工（2 条）：触发场景 + 排队时长 + 投诉升级
- 保修 FAQ（3 条）：进水 / 电池 / 非官方维修
- 商品 SKU FAQ（4 条）：ZP1 防水 / ZP2 Pro 拍照 / 笔记本售后 / 平板配件

总条数：52 → **69** 条（Qdrant 67 → 88 个 chunk）。

### Why
**为什么按"系统功能 → 补数据"反推**：
- KB 不能"为了多而多"——前一轮 67 条覆盖率有 5 个功能空缺
- 系统层面能展示：升级人工（LangGraph V3）/ 发票 / 支付 / 账户 / 商品高频
- **目标**：让使用者问"这个系统能处理 X 场景吗"时，KB 立刻有数据可演示

**为什么不堆数量**：
- 67 → 88 个 chunk，增幅 31%
- 新增 17 条针对 6 个具体功能，**每条都对应一个明确业务场景**
- 避免"重复 FAQ"（如「怎么退款」出现 10 次），保持信息密度

### Tech
- 与现有 schema 完全一致：`{category, doc_type, items: [{source, doc_type, title, text}]}`
- 复用 `scripts/ingest_ecommerce_kb.py`（幂等性靠 uuid5 + MySQL UNIQUE）
- 复用 `scripts/gen_eval_set.py`（Qwen 围绕新 doc 生成 3 query/doc）

### Flow
```
诊断当前 KB → 列出系统功能缺口 → 设计 6 类补全
  ↓
Write 6 个 JSON 文件（17 items）
  ↓
ingest_ecommerce_kb.py → Qdrant 88 chunks（自动切片）
  ↓
gen_eval_set.py → eval_set_v1.json（264 条 query）
  ↓
eval_hitk.py → 新 baseline
```

### 新 baseline（V1.2 之后）

| 指标 | V1.1 (67 doc) | V1.2 (88 doc) | 解读 |
|------|--------------|---------------|------|
| 评估集 | 201 | **264** | +31% |
| hit@1 | 0.517 | **0.485** | 略降（多源稀释）|
| hit@3 | 0.721 | **0.705** | 略降 |
| hit@5 | 0.796 | **0.807** | 略升 |
| hit@10 | 0.900 | **0.883** | 略降 |
| 完全 miss | 20 (10%) | 31 (12%) | 略增 |
| p50 时延 | 207 ms | **195 ms** | 略快 |

**hit@1 略降的根因**：新增 17 条里有 4 条是「商品 SKU FAQ」，商品类目（product_sku001-010）hit@1 一直为 0.0-0.667（多 SKU 文本相似度高，dense vector 难区分）。**这是预期内的 trade-off：换场景覆盖率 → 单点精度稀释**。

### 新类目表现

| 新类目 | hit@1 | hit@5 | 评价 |
|--------|-------|-------|------|
| `faq_sku_002`（ZP2 Pro 拍照）| 1.000 | 1.000 | ⭐ 优秀 |
| `faq_warranty_01`（手机进水）| 1.000 | 1.000 | ⭐ 优秀 |
| `policy_account_faq_01`（换绑手机号）| 1.000 | 1.000 | ⭐ 优秀 |
| `policy_invoice_faq_02`（纸质票丢失）| 1.000 | 1.000 | ⭐ 优秀 |
| `policy_payment_faq_01`（付款未到账）| 1.000 | 1.000 | ⭐ 优秀 |
| `policy_escalation_faq_01`（投诉升级）| 0.667 | 0.667 | ✅ 良好 |
| `policy_payment_main`（支付方式）| 0.333 | 0.667 | ⚠️ 中等 |
| `policy_invoice_faq_01`（电子票查）| 0.333 | 1.000 | ✅ 良好 |
| `faq_sku_003`（笔记本售后）| 0.000 | 0.667 | ⚠️ 待优化 |
| `faq_sku_001`（ZP1 防水）| 0.667 | 0.667 | ✅ 良好 |

### Problem & Fix
**问题：新增「商品 SKU FAQ」类目召回不稳**
- 现象：faq_sku_001/003 等 hit@1 0.0-0.667
- 根因：4 条 SKU FAQ 都含「ZP1/ZP2 Pro/笔记本/平板」型号，dense vector 互相干扰
- 后续优化方向（不立即做）：
  - chunk 切分按「型号 + 场景」切（如「ZP1-防水」「ZP1-充电」各一 chunk）
  - 或加入 BM25 关键词检索兜底（型号名是关键标识）

### Role
**V1.2 = 让 KB 跟系统能力 1:1 对齐**。系统有 6 大能力，KB 就有 6 大类数据。**对外演示时任何功能 demo 都有真实数据可调**——这是"看起来是 demo，跑起来像产品"的关键。

**项目摘要**：
> "构建 88 个 chunk 的电商知识库（6 大类、17 个业务场景），覆盖退换货/物流/促销/保修/发票/支付/账户/升级人工/商品咨询 9 大功能"

---

## 23. M7 RAG 召回优化：商品按场景切分 + Cross-Encoder Rerank（2026-06-27）

### What
针对 hit@K 评估暴露的两类问题，落地两个优化：
1. **Phase A：商品 SKU 按场景切分**（10 SKU → 22 chunks）
2. **Phase C：LLM Cross-Encoder Rerank**（Qwen 二次打分）

交付物：
- `docs/ecommerce_kb/products.json`（重写）：每个 SKU 拆成 2-3 个场景 chunk（overview / 特性 / 保修）
- `backend/app/services/rerank.py`（~160 行）：batch LLM rerank，15 候选/单次 prompt
- `scripts/eval_hitk.py`：加 `--rerank` 标志，支持 A/B 对比

### Why

**为什么分两步**：
- Phase A 是"数据侧"优化：商品类目（product_sku001-010）hit@1 长期 0.0，根因是 10 个 SKU 文本高度相似（"6.7 寸 OLED / 5000mAh"），dense vector 难区分。**按场景切 → 每个 chunk 文本更聚焦 → 相似度可分**
- Phase C 是"算法侧"优化：即使 chunk 切对了，rank=2-10 还有 39.8% 的 query 没在 top-1。**Cross-encoder rerank 把"知道但排不准"的提升到 rank=1**

**为什么用 LLM rerank 不用专门模型**：
1. 零额外依赖（已有 Qwen）
2. 跨语言/多领域适应性好
3. **单 prompt 打分 15 候选**（vs 每候选一次调用）→ 比专门 cross-encoder 还快
4. 讲解亮点：能讲"为什么不选 bge-reranker"（成本/部署/精度 trade-off）

### Tech
- **Qwen plus + temperature=0**（关闭随机性，确保打分稳定）
- **batch prompt 策略**：单次 LLM 调用给 15 候选打分（解析支持 3 种格式：完整 JSON / 简化分数数组 / key:value）
- **MAX_CANDIDATES_PER_CALL=15**（token 上限保护）
- **降级策略**：LLM 调用失败 → 用原始 Qdrant 排序（不崩）

### Flow

```
Phase A：商品按场景切分
  products.json 重写
    SKU001 → overview + camera + battery (3 chunks)
    SKU002 → overview + camera + battery (3 chunks)
    SKU003-SKU010 → overview + 1-2 features
  ↓
  ingest → Qdrant 110 chunks（10 + 12 新）

Phase C：两阶段检索
  query
    ↓
  Qdrant top-15（粗排）
    ↓
  1 次 LLM prompt：给 15 候选打分 [0-10]
    ↓
  按 rerank_score 降序 → top-10（精排）
```

### A/B 对比结果（330 条 query）

| 指标 | V1.2 baseline | V1.2 + rerank | delta | 评估 |
|------|--------------|---------------|-------|------|
| hit@1 | 0.473 | **0.579** | **+10.6pp** | ⭐ 显著提升 |
| hit@3 | 0.715 | **0.806** | +9.1pp | ⭐ 显著提升 |
| hit@5 | 0.803 | **0.861** | +5.8pp | 明显提升 |
| hit@10 | 0.867 | **0.900** | +3.3pp | 略升（接近天花板）|
| miss | 13.3% | **10.0%** | -3.3pp | 减少 1/4 miss |
| p50 时延 | 187ms | 1536ms | +1.35s | 8x（可接受）|
| p90 时延 | 317ms | 3848ms | +3.5s | — |

**hit@1 提升 10.6pp** 来自两个机制：
- 18% 的 query 从 rank=2-3 提升到 rank=1（rerank 把"对但排后"的调到前）
- 7% 的 query 从 rank=4-10 提升到 rank=1（rerank 大幅纠错）

### 按 source 提升最大的类目

| source | baseline | rerank | delta |
|--------|----------|--------|-------|
| product_sku004_sos | 0.333 | 1.000 | **+0.667** |
| product_sku009_connection | 0.333 | 1.000 | **+0.667** |
| faq_top_015 | 0.333 | 1.000 | **+0.667** |
| faq_top_018 | 0.333 | 1.000 | **+0.667** |
| product_sku006 | 0.000 | 0.667 | **+0.667** |
| policy_promotion_coupon | 0.167 | 0.667 | +0.500 |
| faq_top_012 / 025 | 0.667 | 1.000 | +0.333 |

**关键观察**：商品类目（product_sku*）在 Phase A + Phase C 双重优化下 hit@1 从 0.0 提升到 0.667+。说明"商品 SKU 召回差"是 chunking + dense vector 共同问题，需要两端一起治。

### Problem & Fix
**问题 1：早期 batch rerank 触发 Qwen 429 限流**
- 现象：每条 query 并发 15 个 Qwen call × 330 query → 海量并发 → 429
- 修复 1：单 prompt 一次打分 15 候选（330 calls vs 6600 calls）
- 修复 2：parser 容错支持 3 种 LLM 输出格式（实测 LLM 倾向简化输出）

**问题 2：LLM 简化输出格式（只返回 `[7,4,1,1,1]` 而非 `[{"id":0,"score":7},...]`）**
- 修复：parser 优先尝试 list of dicts → 失败则 list of numbers → 失败则正则提取

**问题 3：rerank 后 p50 延迟 1.5s**
- 根因：每次 query 必须等 LLM 响应才能返回
- 接受现状：CS 场景 < 2s 可感知「正常」，1.5s 在边界
- 后续优化（不立即做）：用更小模型（如 qwen-turbo）做 rerank，预期降至 500ms

### Role
**M7 = RAG 模块的"调优层"**。M6 测出"跑得好不好"，M7 负责"让它跑得更好"。这是把"能用"变成"好用"的关键。

**讲解思路**：
- "RAG 召回差怎么办？" → "先看 hit@K 按 source 分布找根因（chunking？同质化？），再针对性修"
- "为什么不用专门 cross-encoder？" → "成本/部署简单/LLM 已够用"
- "rerank 怎么控制成本？" → "单 prompt 批量打分 + 候选截断 + 降级到原始排序"

**项目摘要**：
> "针对商品 SKU 召回差问题，采用「按场景切分 + LLM Cross-Encoder Rerank」两阶段优化，hit@1 从 0.47 提升到 0.58，hit@10 从 0.87 提升到 0.90"

### 最终 V1.2 baseline（含 Phase A + C）

| 维度 | V1.0 | V1.1 | V1.2 baseline | V1.2 + rerank |
|------|------|------|--------------|---------------|
| doc | 52 | 52 | 69 | 69 |
| chunk | 67 | 67 | 88 | 88 |
| eval set | 201 | 201 | 330 | 330 |
| hit@1 | 0.517 | 0.517 | 0.473 | **0.579** |
| hit@10 | 0.900 | 0.900 | 0.867 | **0.900** |

---

## 24. M7 健壮性加固：断路器 + 降级 + SSE heartbeat（2026-06-28）

### What
针对"生产级可用性"补齐 3 个核心模块的健壮性：
- **断路器通用工具**：`app/core/circuit_breaker.py`（CLOSED/OPEN/HALF_OPEN 状态机）
- **Qdrant 断路器降级**：search 返回 [] / upsert 返回 0（让 RAG 走 LLM 兜底）
- **embedding retry + 超时**：429/超时重试 1/2/4s，总失败 → EmbeddingError
- **SSE heartbeat + 断开检测**：30s 心跳 + asyncio.CancelledError 处理 + closed 事件

交付物：
- `app/core/circuit_breaker.py`（~180 行）
- `app/clients/qdrant.py`：加断路器 + health_check()
- `app/core/embedding.py`：加 retry + EmbeddingError + embed_text_or_mock
- `app/api/chat.py`：改 async generator + heartbeat + 断开检测
- `tests/test_robustness.py`（~280 行，**20 个测试全过**）

### Why
**为什么用断路器（不用 try/except）**：
- 防止级联故障：Qdrant 慢响应会占满线程池，触发雪崩
- 智能恢复：自动从 OPEN → HALF_OPEN 探活，比手动开关更可靠
- 可观测：每次状态切换 WARNING log，失败计数导出

**为什么 embedding 用 retry + EmbeddingError（不用断路器）**：
- embedding 单次调用 < 200ms，3 次重试最多 7s（可接受）
- 断路器适合"反复失败的慢依赖"，embedding 是"偶发限流的快依赖"
- 总失败抛 EmbeddingError 让上层显式处理（不能静默失败 → 污染 RAG）

**为什么 SSE 加 heartbeat**：
- nginx 默认 `proxy_read_timeout=60s`：SSE 长连接无数据 60s 会被 nginx 切
- 每 30s 发 heartbeat < 60s 阈值，连接保持
- 客户端断开时 `request.is_disconnected()` 感知 → 跳出循环 + 写审计

### Tech
- **断路器状态机**：3 状态 + lock 保护 + 懒检查 OPEN → HALF_OPEN
- **embed retry**：指数退避 1/2/4s + 区分可重试（429/timeout/conn）/不可重试（401/参数错）
- **SSE async generator**：asyncio.to_thread 包装同步 Synthesizer.run_stream + 30s wait_for 节流
- **降级策略对比**：

| 故障 | 降级 | 用户感知 |
|------|------|---------|
| Qdrant 挂 | search 返回 [] | 答非所问（但有响应）|
| Qdrant 挂 | upsert 返回 0 | MySQL 仍有数据 |
| Qwen 429 | 重试 3 次 | 延迟 +1-7s（可接受）|
| Qwen 全挂 | EmbeddingError | 上层 try/except 处理 |
| Embedding 失败 | embed_text_or_mock 返回零向量 | ⚠️ 仅用于非 RAG 场景 |
| 客户端断开 | asyncio.CancelledError | 跳出循环 + 写审计 |

### 测试覆盖（20/20 PASS）

| 模块 | 测试数 | 关键场景 |
|------|-------|---------|
| CircuitBreaker 状态机 | 9 | CLOSED/OPEN/HALF_OPEN 转换 + 探活成功/失败 + reset |
| Qdrant 降级 | 3 | search 降级 / upsert 降级 / health_check |
| Embedding 降级 | 4 | 429 retry / 总失败 / 零向量 / 空文本 |
| SSE heartbeat | 4 | interval 常量 / 事件格式 / heartbeat / closed |

### 真服务验证

- 重建 Docker API 容器（新 SSE 代码 + 断路器）
- curl /chat 流式：meta → token*24 → done → closed（无 error，正常结束）
- /health 端点：Qdrant/Redis/MySQL 全 ok

### Problem & Fix

**问题：SSE async generator 把 `(None,)` 误当 tuple 解包**
- 现象：`next(sync_iter, None)` 返回 None（耗尽哨兵），被 `event_type, data = item` 解包报 TypeError
- 修复：先判 `if item is None: break`，再解包
- 测试覆盖：原有 synthesizer 测试仍全过；新增 robustness 测试覆盖 sentinel 逻辑

### Role
**M7 = 让系统从"能跑"变"跑得稳"**。前面 M1-M6 实现功能 + 量化效果，M7 加固健壮性。**这是对外"生产级"问题的标准答案**：
- "Qdrant 挂了怎么办？" → "断路器开路 → RAG 返回空 → LLM 走工具兜底"
- "LLM 限流怎么办？" → "指数退避重试 3 次 → 总失败抛业务异常让上层处理"
- "SSE 长连接被 nginx 切断怎么办？" → "30s heartbeat + 客户端断开检测"
- "为什么不直接 try/except？" → "断路器防雪崩、retry 防偶发、heartbeat 防超时——各有分工"

**项目摘要**：
> "实现 Circuit Breaker 模式保障 Qdrant 依赖可用性（3 状态机 + 30s 探活），SSE 长连接加 30s heartbeat 防止 nginx 切断，Embedding 服务加 429 指数退避重试，配套 20 个单元测试覆盖各降级路径"

## 25. M8 可观测性：Request ID 全链路追踪 + 业务指标埋点（2026-06-28）

### What

为生产化铺路，补齐可观测性三大缺口：
1. **Request ID 全链路追踪**：每个 HTTP 请求生成 / 透传唯一 ID，所有日志自动带上
2. **结构化日志**：JSON / 文本双模式（dev=text, prod=json），按 APP_ENV 自动切
3. **业务指标埋点**：`/metrics` 端点导出 chat / RAG / embedding / hit@K 实时统计

### Why

**之前痛点**：
- 线上报障只能拿到 session_id，但 30+ 个 logger 调用里查日志是灾难（grep 不到上下文）
- 想看 chat 多少 QPS / p90 多快 / RAG 失败率 → 必须进 MySQL 写 SQL 跑聚合（不能实时）
- hit@K 只有离线 eval_hitk.py 能算，线上召回质量下降几天才发现

**M8 解决**：
- Request ID 串联所有日志（grep 一个 id 拿全链路）
- ContextVar 自动注入 session_id / user_id 到所有 log（无需每个 logger.info 重复带）
- /metrics 端点秒级回显业务健康度（命中 Grafana / curl 即可看）

### Tech

| 组件 | 选型 | 理由 |
|------|------|------|
| Request ID 传播 | `contextvars.ContextVar` | asyncio 原生支持，per-task 隔离 |
| 日志格式 | `logging.Formatter` 自定义 | 不引入 structlog（多一依赖） |
| 指标存储 | 内存 dict + `threading.Lock` | 不引入 Prometheus（CLAUDE.md 禁新基础设施） |
| 端点 | `GET /metrics` 返回 JSON | 与现有 API 风格一致，前端 / curl 直接消费 |
| 中间件 | Starlette `BaseHTTPMiddleware` | 双 middleware（RequestId 内层 + ResponseHeader 外层） |

### Flow

```
HTTP 请求进入
    ↓
ResponseHeaderMiddleware (外层)
    ↓
RequestIdMiddleware (内层)
    ↓ 提取 X-Request-Id / 生成 UUID
    ↓ set_request_id(rid) → ContextVar
    ↓ logger.info("GET /chat 200", extra={method, path, status, duration_ms})
    ↓ 自动带 [req=xxx sid=xxx uid=xxx]
    ↓
Router handler (chat.py)
    ↓ set_session_id(sid), set_user_id(uid) → 业务日志自动带
    ↓
Synthesizer.run_stream (PolicyService.search_policy 路径)
    ↓ metrics.record_retrieve_hits(5)  ← hit@K 埋点
    ↓ metrics.record_hit_at_k(1)      ← 有结果算命中
    ↓
响应流回传
    ↓
ResponseHeaderMiddleware 读 ContextVar → 写 X-Request-Id 响应头
    ↓
客户端拿到 X-Request-Id: xxx
```

### 指标字段（`/metrics` JSON）

```json
{
  "uptime_seconds": 17.4,
  "chat": {
    "total": 5,
    "by_intent": {"policy_query": 5},
    "latency_ms": {"p50": 1805.8, "p90": 2037.7, "max": 2066.7, "samples": 5},
    "answer_tokens_total": 665,
    "retrieve_hits_avg": 5.0
  },
  "rag": {"qdrant_search_total": 5, "qdrant_search_success": 5, "qdrant_fallback_open_total": 0, "qdrant_error_total": 0},
  "embedding": {"calls_total": 5, "retries_total": 0, "errors_total": 0},
  "circuit_breaker": {"qdrant": {"state": "closed", "failure_count": 0}},
  "hit_at_k": {"window_size": 5, "hit@1": 1.0, "hit@3": 1.0, "hit@5": 1.0, "hit@10": 1.0}
}
```

### Problem & Fix

| 问题 | 解决 |
|------|------|
| `RequestIdMiddleware` 写响应头失败（中间件顺序） | 双 middleware：RequestId（内层）先 set ContextVar，ResponseHeader（外层）再读出来写头 |
| `/health` 注入 circuit_breaker 字段后 `all(c["status"]=="ok")` 抛 KeyError | circuit_breaker 提到顶层独立字段，不参与 overall 判定 |
| `synthesizer.run_stream` 调 `reset_intent(token)` 抛 `ValueError: Token created in different Context` | synthesizer 在 to_thread 跑的 sync generator，ContextVar token 跨 thread context 不可 reset。改用 `logger.info(..., extra={"intent": intent})` 显式传 intent |
| `chat.py` SSE 流式 chat 不走 `pipeline.run_stream`（走 PolicyService），导致 hit@K 不计数 | 给 PolicyService.search_policy 也加 `metrics.record_retrieve_hits / record_hit_at_k` |
| Metrics 单例污染测试 | 测试用 `Metrics()` 新实例，不用 `metrics` 全局单例 |

### Role

M8 是 V1.x → V2.0 生产化的最后一块拼图：
- M7 = 高可用（挂了能恢复）
- M8 = 可观测（挂了能定位 + 提前预警）
- 两者一起让系统从「能跑」升级到「能运维」

### 配套测试

30 个新测试覆盖：
- ContextVar set/get/reset + 跨 asyncio task 隔离（5 个）
- JSONFormatter 输出格式（5 个）
- ContextFilter 自动注入（1 个）
- setup_logging 初始化（2 个）
- Metrics 计数器 / 直方图 / hit@K 计算 / 线程安全（10 个）
- RequestIdMiddleware 生成 / 透传 / 跳过 health / metrics（3 个）
- /metrics 端点字段完整性（1 个）

**总计 75 个测试全部通过**（45 老 + 30 新）。

> 2026-07-05 更新：随着新增 IntentService / RefundGraph / Rerank / Healthcheck 等模块，pytest 累计 122 个，本节"75 个"为该阶段历史快照。

### 讲解思路

- "怎么排查线上问题？" → "Request ID 串联所有日志，curl /metrics 实时看业务健康度"
- "为什么不用 Prometheus？" → "MVP 阶段内存足够，加 Prometheus 要拉新基础设施（CLAUDE.md 禁止），后续量起来再迁"
- "hit@K 线上怎么算的？" → "用'检索到结果'作命中代理，真 gold label 在离线 eval_hitk.py 算"
- "ContextVar 和线程局部变量区别？" → "asyncio task 隔离 + 跨 await 自动传播，TLGV 不行"

**项目摘要**：
> "为 RAG 客服系统加可观测性：Request ID 中间件实现全链路日志追踪（ContextVar + 双 Middleware 透传），结构化 JSON 日志 + 业务指标埋点（chat / RAG / embedding / hit@K），新增 `/metrics` 端点 + 30 个单元测试，覆盖 5 个业务模块（合成器 / 检索 / 嵌入 / 客户端 / API）"

---

## 9. M9 前端升级 - 从 demo 到电商产品

**模块**：frontend（M9 全量重构）/ backend/app/api/shop.py / backend/app/api/conversations.py (PATCH)

### What
把"最小可运行 demo"（1174 行 / 6 组件 / 一个聊天框）升级成"接近真实电商客服产品"的全栈前端：路由化、注册 + 登录、商品橱窗 + 详情、个人中心、会话管理、消息卡片嵌入、演示模式首页。后端补 4 个公开端点 + 1 个 PATCH 改标题。

### Why
- 当前前端给外部访问者（使用者 / GitHub demo）只看到「套了紫色 logo 的聊天框」，完全感受不到「电商客服」定位
- 用户 5 点痛点：UI 不真实、历史记录没意义、没有注册、只有一个聊天界面、没有展示平台
- 真实电商客服（京东 / 淘宝小蜜 / Shopify Chat）都有：商品上下文、订单上下文、个人中心、注册流程、悬浮 CTA

### Tech Stack
- **新增依赖**：vue-router 4.6.4（路由化）
- **新增组件**：AppNav / ProductCard (3 density) / OrderCard (2 density) / MessageCard
- **新增视图**：LoginPage / DemoLanding / ShopPage / ProductDetail / ChatPage / ProfilePage
- **后端**：FastAPI 4 个新端点 (`/products` `/products/{sku}` `/orders/my` `/orders/{order_no}`) + 1 个 PATCH (`/conversations/{sid}`)
- **类型系统**：TypeScript + vue-tsc 严格模式
- **图片**：3 tier fallback (Unsplash → dummyimage → SVG 渐变)，最终用 dummyimage 占位图（类目色 + SKU 文字）

### Flow
1. 用户访问 `/demo`（未登录也可看）→ 看到 hero + 能力卡片 + 指标 + CTA
2. 点「注册」→ `/login?tab=register` → 注册成功自动登录 → 跳 `/shop`
3. 商品橱窗选品 → 点「问问客服」→ 跳 `/chat?q=SKUxxx+怎么样` → 自动发问
4. 流式 SSE 返回 → assistant 消息下方自动渲染 ProductCard / OrderCard（按 intent）
5. 侧边栏自动按「今天/昨天/本周/更早」分组，hover 出 × 删单条，顶部一键清空
6. 顶栏头像菜单 → 个人中心 → 看到订单列表 + 统计 + 退出

### Problem → Fix
| 问题 | 根因 | 修复 |
|------|------|------|
| npm run build 报 `hit@1` 属性语法错 | TS 不允许 `@` 在 identifier | 改用 `'hit@1'` 字符串 key |
| npm run build 报 `OrderDetail` 在 api.ts 未导出 | 误从 api.ts import，应该从 types.ts | 改 import source |
| npm run build 报 DemoLanding `document` not found | Vue 模板不暴露 `document` 全局 | 改用 computed 包装 |
| `/products` 返回 404 | Dockerfile COPY 烘入 image，未重启不生效 | `docker compose build api && up -d api` |
| PATCH 内联中文 body 解析失败 | Windows bash 中文编码（已知问题） | `--data-binary @file` 走文件 |
| 老 ChatPage / LoginForm 没人引用但存在 | M9 已迁到 views/ | 加 `@deprecated` 注释保留作历史 |

### 关键设计决策
| 决策 | 选择 | 原因 |
|------|------|------|
| 状态管理 | 不上 Pinia | 小项目 composable + ref 足够，引入会增加复杂度 |
| 商品图 | dummyimage 渐变 + SKU 文字 | Unsplash SSL 失败 + Picsum 随机图不像产品 |
| 卡片密度 | ProductCard 3 密度 / OrderCard 2 密度 | 橱窗 / 详情 / 消息内复用同一组件 |
| 自动标题 | 前端 PATCH 覆盖后端默认 200 字 | 后端默认存 first_query[:200]，UI 显示应≤20 字 |
| 消息卡片 | 追加到气泡下方，不替换正文 | 用户先看 LLM 答得对不对，卡片辅助理解 |

### Scope Lock 放宽理由
按 CLAUDE.md §5「单次只允许改一个模块」，但用户明确说「全做」。
实际操作：分 9 阶段（基础设施 → 后端 API → 注册 → 卡片 → 会话管理 → 个人中心 → 橱窗 → demo 首页 → 构建），仍按模块顺序推进，最后一次性提交。

### Role
M9 是 V1.x → V2.0 商业化的最后一块：
- M1-M5 = 后端核心能力（RAG / 意图 / 多意图 / 退款 / 性能）
- M6-M8 = 后端生产化（V3 LangGraph + 可观测 + JWT）
- **M9 = 前端从 demo 到产品**

后端从「能跑」升级到「能生产」，前端从「能演示」升级到「能给客户用」。

---

## 10. M12 Query Rewriter - 多轮对话指代补全

**文件**：`backend/app/services/query_rewriter.py`（新建）+ `backend/app/services/synthesizer.py`（run_stream 入口 +6 行）+ `backend/app/services/metrics.py`（+rewrite 字段/方法/snapshot）

### What
多轮对话的 query 改写：把用户问题里的指代词（"它/这个/那个/刚才/那种"等）补全为具体实体（商品名/SKU/订单号/颜色等），让 RAG 检索 query 直接命中正确知识库文档。三层防浪费：L0 规则检测（零成本短路）+ L1 history 检查（无 history 跳过）+ L2 LLM 改写（条件触发，1 次 qwen chat 调用 ~250 token）。

### Why
- 真实多轮对话里 30%+ 的 query 含指代词，但当前 query 直接送 embedding → 检索召回错（"它能便宜吗" embedding 不到 "iPhone 15 Pro"）
- 现有 history 只用作 LLM prompt 上下文（让 LLM "看懂"对话），不参与 embedding 检索 → 治标不治本
- Multi-Query / HyDE / Step-back 成本高收益场景依赖度高，先做最高频痛点（指代补全）
- 规则前置过滤：含指代词才调 LLM，避免无谓 token 浪费

### Tech Stack
- **正则表达式** `COREFERENCE_PATTERNS`：覆盖电商高频指代词（它/他们/这个/那个/这些/那些/刚才/之前/上面/下面/那款/这款/这种/那种/前者/后者 等）
- **qwen chat**（`core/qwen.py` 已封装）：`temperature=0.0` + `max_tokens=80`（输出短）
- **短路降级**：LLM 返回空 / 输出过长（> 3 倍原 query）/ 异常 → fallback 返原 query
- **埋点**：`metrics.inc_rewrite(reason)` 区分 `rewritten` / `skipped_no_coref` / `skipped_no_history` / `error_*`

### Flow
```
用户 query → L0 正则检测指代词
   ├─ 无 → inc_rewrite("skipped_no_coref") → 返原 query
   └─ 有 → L1 history 非空检查
              ├─ 空 → inc_rewrite("skipped_no_history") → 返原 query
              └─ 非空 → L2 调 qwen chat（prompt = system + history + query）
                          ├─ 成功 → inc_rewrite("rewritten") → 返改写后
                          └─ 异常/空/过长 → inc_rewrite("error_*") → 返原 query
```

### 插入点
`synthesizer.py` 的 `Synthesizer.run_stream` 入口（line 281-291），intent classify **之前**：
- product_query / policy_query 走 `PolicyService.search_policy(query)` → 改写有效
- order_query / refund_query 走 tool 查 DB（不读 query 检索）→ 改写无效但无害
- intent 分类前调用：避免「它」「这个」被识别成无效 query

### Problem → Fix
| 问题 | 根因 | 修复 |
|------|------|------|
| `metrics.metrics.rewrite_by_reason` AttributeError | `__pycache__` 旧 .pyc 缓存了老 metrics.py | `find ... __pycache__ -exec rm -rf` 清缓存后正常 |
| 中文乱码（`iPhone 15 Pro�ܱ��˵���`） | Windows GBK 终端显示问题 | 仅终端显示问题，不影响 result 字符串内容 |
| 改写结果过长可能失控 | LLM 输出没长度约束 | 加防护：`len(rewritten) > len(query) * 3 + 50` → fallback |

### 验证（11/11 PASS）
- `scripts/verify_rewriter_mock.py`：mock qwen_chat 跑 9 个 case
  - 含指代+history → 改写成功、结果含 history 实体
  - 无指代词 / 无 history / 长 query 无指代 → 短路跳过
  - 复杂指代（这个和那个）→ 改写成功
  - LLM 异常 / 输出过长 / 返回空 → 降级返原 query
  - 空字符串 / None query → 直接返空/None

### Architecture Role
- 属于 `services/` 编排层，按 §6 规则：只调 core/qwen.py + services/metrics.py
- 不动 api/chat.py / intent_service / policy_service
- 单一职责：只做指代补全，不做 Multi-Query / HyDE / Query 扩展（YAGNI）

### 关键设计决策
| 决策 | 选择 | 原因 |
|------|------|------|
| 模块位置 | services/query_rewriter.py | services/ 编排层，符合 §6 |
| 触发条件 | 含指代词 + history 非空 | 零浪费：无指代词直接跳过 |
| 改写方式 | 单次 LLM（temp=0, max_tokens=80） | 指代补全只需短输出 |
| 失败降级 | 返原 query + warning log | 不阻塞业务 |
| Scope Lock | 4 文件（rewriter + metrics + synthesizer + verify） | 不动 api/chat.py / intent / policy |

### Role
M12 是 RAG 链路最后一公里优化：
- M1-M5 = 后端核心能力
- M6-M8 = 后端生产化（V3 LangGraph + 可观测 + JWT）
- M9 = 前端从 demo 到产品
- M11 = 输入防御（InputGuard 防 token 滥用）
- **M12 = RAG 召回优化（query 改写，指代补全）**

让"用户问得越随意，系统召回越准"。

---

## 26. Sprint 2 Prompt 基础设施（2026-07-12）

**文件**：`backend/app/services/prompt_loader.py`（194 行）+ `backend/config/prompts/` + `tests/test_prompt_loader.py`（21 用例）

### What
新增统一 Prompt 加载器（Protocol + YAML 实现 + 工厂）：
- `PromptLoader` Protocol（`@runtime_checkable`）
- `YAMLPromptLoader` 基于文件系统 + mtime 缓存（热更新免重启）
- `get_prompt_loader()` 工厂入口（单例 + 依赖倒置）
- 4 个自定义异常（`PromptError / PromptNameError / PromptNotFoundError / PromptFormatError`）
- 配套 `Settings.PROMPT_DIR` 配置 + Dockerfile `COPY config/` 一行

### Why
- 当前 5 处业务 Prompt 散落在 synthesizer / intent / query_rewriter / rerank / guard 代码中（**G5 硬编码缺口**），无法版本管理 / 热更新 / 多租户覆盖
- S2 只搭架子（不动业务），S3 拆 synthesizer 时顺手抽 — 单 Sprint 推不动跨 5 个模块改 + 928 行拆分
- 加载器是"独立可单测"模块：纯文件 I/O + 缓存策略，不依赖 LLM / Embedding / 数据库

### Tech Stack
- **PyYAML 6.0.2**（锁版本，Sprint 2 唯一新依赖）
- **Protocol + Factory**（沿用 Sprint 1 Provider 抽象的模式）
- **threading.Lock**（单进程读多写少场景；写并发留 V3+）
- **pytest tmp_path fixture**（文件隔离测试）

### Flow
```
业务调用 get_prompt_loader().load("intent")
    ↓
_resolve_base_dir() 解析 PROMPT_DIR（env 覆盖 > 相对 backend 根）
    ↓
name 白名单正则 + resolve 后前缀双重校验
    ↓
exists() 检查 → 不存在抛 PromptNotFoundError
    ↓
stat().st_mtime  对比缓存 → 命中直返；否则 yaml.safe_load
    ↓
data["content"] strip → 缓存 + 返回
```

### Problem → Fix
- **路径越权风险**：`name = "../etc/passwd"` 可能绕过白名单
  - 防御：双层校验 + 双重前缀匹配（Windows `\` 和 Unix `/` 都覆盖）
- **mtime 测试 flaky**：Windows 100ns 精度 / Linux ext4 1s 精度不一致
  - 解决：`time.sleep(0.05)` 让 fs 刷新 mtime（双平台稳）
- **stat() 顺序 bug**：`stat()` 在 `exists()` 之前调用导致文件不存在时抛 `FileNotFoundError` 而非业务异常
  - 修复：测试第 1 次发现 → 第 1 次修复（最小改动：把 exists() 移前）
- **PROMPT_DIR 相对路径与 cwd 耦合**：本地 / 容器 / 测试 3 种 cwd 行为不一致
  - 解决：用 `Path(__file__).resolve().parents[2]` 计算 backend 根，env 覆盖支持绝对路径

### Architecture Role
属于 `services/` 层（业务基础设施），按 §9.7 Interface First 落地：
- 业务模块 → `from app.services.prompt_loader import get_prompt_loader`（依赖抽象）
- 当前唯一实现：`YAMLPromptLoader`（基於 YAML 文件）
- 未来扩展位：多租户覆盖（V3+）→ 加 `TenantAwarePromptLoader`，工厂按 context 切换

**Phase 1 进度**：S1 ✅ + S2 ✅ = 2/3；S3（拆 synthesizer + 抽 5 个 prompt）待启动。

---

> 提示：Sprint 3 启动时新建 `docs/decisions/2026-XX-XX-sprint-3-synthesizer-split.md`；
> 5 个业务 prompt YAML 命名沿用 `config/prompts/README.md` 约定（intent.yaml / rerank.yaml / ...）。

---

## 27. Sprint 3 Synthesizer 拆分（928 → 5 模块）（2026-07-12）

**文件**：
- `backend/app/services/chat/`（子包 6 个文件，共 1056 行）
  - `__init__.py`（空，0 行）
  - `orchestrator.py`（402 行，Synthesizer 主类）
  - `prompt_assembler.py`（276 行，7 个纯字符串处理函数）
  - `stream_dispatcher.py`（78 行，stream_llm + 滑窗检索）
  - `refund_handler.py`（222 行，handle_refund_v2/v3）
  - `citation_formatter.py`（14 行，占位 + 扩展注释）
- `backend/app/services/synthesizer.py`（928 → 64 行，薄壳 re-export）
- `backend/app/api/chat.py`（import 路径切换）
- `backend/config/prompts/agent.yaml` + `no_login.yaml`（Range A 抽取 2/5）
- `backend/tests/test_chat_prompt_assembler.py`（11 用例）+ `test_chat_meta_contexts.py`（7 用例）

### What
按 `docs/decisions/2026-07-12-sprint-3-synthesizer-split.md` 5-commit 计划落地：
1. **commit 1（ADR）**：明确范围 A 决议（仅抽 2/5 Prompt，业务逻辑零变更）
2. **commit 2（YAML）**：把 synthesizer.py:42 SYSTEM_PROMPT_BASE / :53 NO_LOGIN_PROMPT 提到 YAML
3. **commit 3（cp 安全网）**：完整 cp 4 个新模块到 `chat/` 子包，不动旧代码
4. **commit 4（切换）**：api/chat.py 切换到 `chat.orchestrator`，旧 synthesizer 改薄壳，refund_v2/v3 移到 `chat/refund_handler.py`
5. **commit 5（测试+文档）**：18 个纯函数单测 + 文档收尾（本节）

**新模块边界**：
| 模块 | 职责 | 行数 |
|------|------|------|
| `orchestrator.py` | Synthesizer.run_stream 主流程 + 4 个 `_handle_<intent>` 意图分发 | 402 |
| `prompt_assembler.py` | `_build_chat_prompt` / `_format_tool_result` / `_format_policy_docs` / `_format_history` / `_build_meta_contexts` / `_extract_order_no_from_history` | 276 |
| `stream_dispatcher.py` | `stream_llm` / `stream_simple` / `search_by_keyword_window` + `_LLM_SEMAPHORE` | 78 |
| `refund_handler.py` | `handle_refund_v2`（V2.x 双轨制）+ `handle_refund_v3`（V3 LangGraph） | 222 |
| `citation_formatter.py` | 占位（未来 citation 渲染扩展位） | 14 |

**已知预算偏离**（ADR §6 标注）：
- orchestrator.py 402 vs ADR 预算 < 350（+52）
- prompt_assembler.py 276 vs ADR 预算 < 250（+26）
- chat/ 6 文件 vs ADR 预算 ≤ 4（+2）
- 原因：orchestrator 主类承担太多意图分发（M9.5+ 防串单、多意图路由、退款 V2/V3 选择）；二次拆分需要等 S4 业务规则 YAML 化后才能继续拆。

### Why
- **G5 硬编码缺口**：当前 5 处业务 Prompt 散落在 synthesizer / intent / query_rewriter / rerank / guard 代码中，无法版本管理 / 热更新 / 多租户覆盖
- **G7 单文件过大**：synthesizer.py 928 行（含 5 个 prompt + 4 个意图分支 + 退款双轨 + 流式 + 元数据 + 兜底回答），单文件维护窗口已超限
- **S2 → S3 分阶段**：S2 先搭 prompt_loader 架子 + 缓存 + 防御；S3 在 S2 基础上拆 synthesizer 同时抽 2 个 synthesizer 范围内的 Prompt（agent + no_login），跨模块的 3 个 intent / query_rewriter / rerank 留 S4
- **Range A 决议**：单 Sprint 同时拆 928 行 + 抽 5 个跨模块 Prompt 工作量爆炸；用户决议"先拆 synthesizer，抽 2/5 Prompt（synthesizer 范围内），余下 3 个降级 Sprint 4"

### Tech Stack
- **薄壳 re-export 模式**：synthesizer.py 改为纯 re-export，兜住历史 import 路径（`from app.services.synthesizer import Synthesizer` 仍可用）
- **模块就近原则（§7.3）**：调用方只 import `chat.orchestrator.Synthesizer`；chat 子包内部模块之间就近引用
- **Python parenthesis 字符串拼接**（YAML 多行内容 + 注释保留技巧）：
  ```python
  SYSTEM_PROMPT_BASE = (
      get_prompt_loader().load("agent")  # 业务提示词来自 YAML
  )
  ```
- **YAML block string**（`content: |` 保留多行缩进 + 不替换转义）
- **pytest 纯函数测试**（无 I/O / 无 DB / 无 LLM 依赖，可独立跑）

### Flow
```
旧：api/chat.py → Synthesizer.run_stream → (synthesizer.py 内: prompt + 4 个 _handle_* + refund_v2/v3 + stream_llm)
新：api/chat.py → chat.orchestrator.Synthesizer.run_stream
       ↓
       _handle_<intent>  （chat/orchestrator.py）
       ↓
       chat.prompt_assembler._build_chat_prompt  （7 段优先级拼接）
       ↓
       chat.stream_dispatcher.stream_llm  （调 get_llm_provider）
       ↓
       SSE (meta/token/done)

退款分支：api/chat.py → chat.refund_handler.handle_refund_v3 → refund_graph_app.stream → （异常 fallback）→ handle_refund_v2
```

### Problem → Fix
- **16 个测试失败**（commit 4 切换后）：
  - **根因 1**：test patches `app.services.synthesizer.OrderService` 不拦截 `chat.refund_handler.OrderService`（不同 binding）
  - **修复 1**：所有 3 个测试文件（test_anti_hallucination / test_source_attribution / test_synthesizer_refund）的 patches 改打到 `chat.refund_handler.*` / `chat.orchestrator.*` / `chat.stream_dispatcher.*` 三个 namespace
  - **根因 2**：`Synthesizer._handle_refund_v3(...)` 直接调用不存在（refund 方法已移到 chat.refund_handler 模块级函数）
  - **修复 2**：tests 改为 `from app.services.chat.refund_handler import handle_refund_v3; handle_refund_v3(...)`
  - **根因 3**：`@patch("app.services.chat.orchestrator.get_llm_provider")` 报 AttributeError
  - **修复 3**：get_llm_provider 由 chat.stream_dispatcher import，不在 chat.orchestrator 命名空间；patch 改打到 stream_dispatcher
  - **根因 4**：MySQL OperationalError（test_product_not_found_no_llm）
  - **修复 4**：stream_dispatcher 与 orchestrator 各自 import ProductTool，独立 binding；tests 必须 patch 两个位置

- **orchestrator.py 596 行 > 350 ADR 预算**：二次拆分 `_handle_refund_v2/v3` 到 `chat/refund_handler.py`，降到 402 行（仍超 52 行，记录到 roadmap 已知偏离）

- **第一次薄壳设计误判**：一度把 settings / OrderService / PolicyService / RefundService / v12_rag_run_stream / get_llm_provider 也加进 synthesizer.py re-export，让测试走老路径
  - **反思**：违反"单一源真（single source of truth）"原则；最终选择迁移测试到 chat.* 命名空间而非让薄壳变厚

- **M13 订单号字母后缀**：用户订单号格式 `ORD20260704899EBA` 包含字母，原始正则 `ORD\d+` 会截断
  - **修复**：正则改为 `ORD\d{8}[A-Z0-9]{3,6}`（8 位日期 + 3-6 位字母数字混合），由 M9.5+ 在 prompt_assembler 里兜底提取

### Architecture Role
属于 `services/` 层（业务编排核心），按 §7.3 模块就近原则：
- **调用方**：`api/chat.py` 只 import `chat.orchestrator.Synthesizer`（不直接进 chat 子包内部模块）
- **子包内部**：orchestrator / prompt_assembler / stream_dispatcher / refund_handler 互为就近依赖（同包内 import）
- **降级兜底**：`synthesizer.py` 薄壳 re-export 保证下游 0 改动升级；删除计划 = **S4 末**
- **测试架构**：单文件纯函数测试（test_chat_prompt_assembler / test_chat_meta_contexts）与 Mock 集成测试（test_synthesizer_refund）分层；纯函数测试覆盖契约，集成测试覆盖流式协议

**Phase 1 进度**：S1 ✅ + S2 ✅ + S3 ✅ = 3/3；S4（业务规则 YAML 化 + 余 3 Prompt 抽取 + 删除 legacy 薄壳）待启动。

---

**Sprint 3 关键决策回顾**（详见 ADR）：
| 决策点 | 决议 | 理由 |
|--------|------|------|
| 拆分粒度 | 5 模块（orchestrator + prompt_assembler + stream_dispatcher + refund_handler + citation_formatter） | 按职责聚合，4 模块时 refund 流占 orchestrator 60% 太重 |
| Prompt 抽取范围 | Range A：仅 2/5（agent + no_login 在 synthesizer 范围内） | 单 Sprint 工作量爆炸；余 3 个跨模块的留 S4 |
| 旧 synthesizer 处理 | 薄壳 re-export（64 行） | 下游零改动升级 + 删除计划 S4 末 |
| 退款双轨制 | V2/V3 整块移到 chat/refund_handler.py（不在 orchestrator） | 退款业务独立、LangGraph 异常 fallback 逻辑自成一体 |
| 二次拆分 | 推迟到 S4 | orchestrator 二次拆需先 YAML 化业务规则（阈值 / 转人工 / 退款判定） |

---

## 28. Sprint 4 业务规则 YAML 化 — 阶段 1/2/3（2026-07-13）

**文件**：

| 类型 | 路径 | 说明 |
|------|------|------|
| 新增 YAML | `backend/config/business_rules/refund.yaml` | 阶段 3 新增，2 字段 |
| 新增加载器 | `backend/app/services/config_loader.py` | 阶段 1 新增，Protocol + 工厂 + 单例 + 异常体系 |
| 新增测试 | `backend/tests/test_refund_config.py` | 阶段 3 新增，9 用例（3 文件 YAML 同步 / 公共 API / fail-fast） |
| 改造 | `backend/app/services/refund_graph.py` | 顶部 2 常量 → YAML 引用 |
| 改造 | `backend/app/tools/refund_tool.py` | `RefundTool.REFUND_WINDOW_DAYS` 类属性 → YAML 引用 |
| 改造 | `backend/app/services/order_lifecycle.py` | `DELIVERY_OFFSET_DAYS` 模块级常量 → YAML 引用 |
| 修复 | `backend/tests/test_guard_config.py` | 加 post-only autouse fixture + 放宽 `is` 断言为 `==` |

### What
Sprint 4 分阶段落地业务规则配置化（roadmap §3.5）：
- **阶段 1**（commit `38932ab`）：新增统一加载器 `config_loader.py`，提供 Protocol 抽象（CLAUDE.md §9.3.3）+ 工厂单例 + name 白名单 + 路径越权拦截 + fail-fast 异常体系
- **阶段 2**（commit `5132176`）：guard.py 7 个阈值 + 6 条闲聊话术迁出到 `guard.yaml`，首次验证 config_loader 通路
- **阶段 3**（commit `70e5a3e` + `efa729b`）：**跨 3 文件共享常量**迁出到 `refund.yaml`，单一真相源落地 + 修复 test 污染

### Why
- **G8 业务规则配置化**：CLAUDE.md §9.4.2 禁止业务规则硬编码；改阈值需改 YAML + 重启，不需改代码
- **3 文件常量重复定义（关键发现）**：`REFUND_WINDOW_DAYS = 7` 在 `refund_graph.py` + `refund_tool.py` 两处硬编码；`DELIVERY_OFFSET_DAYS = 2` 在 `refund_graph.py` + `order_lifecycle.py` 两处硬编码 — 迁移单个文件会保留"双真相源"风险，按 CLAUDE.md §5.2 跨模块四要素一次迁移

### Tech Stack
- **PyYAML**（已锁 6.0.2，Sprint 2 引入）
- **Protocol + runtime_checkable**（CLAUDE.md §9.3.3 抽象模式，业务模块通过 `get_config_loader()` 获取实例，禁止直接 `new`）
- **dict 缓存 + threading.Lock**（启动期一次加载，roadmap §3.5 不参与热更新）
- **三层防御**（path resolve + name 白名单正则 + base dir 越权检查）— 防 name 注入

### Flow
```
get_config_loader() → _resolve_base_dir() 读 settings.BUSINESS_RULES_DIR
    ↓ (绝对路径直用 / 相对路径解析 backend 根)
YAMLConfigLoader(base_dir).load(name)
    ↓
1. name 白名单校验 ^[a-z0-9_]+$
2. 路径 resolve 后越权检查
3. 查 _cache → 命中直接返回
4. 未命中：full_path.exists() 检查 → 解析 YAML → 顶层 dict 校验 → 写入 _cache
    ↓
返回 dict（业务模块顶部一次性解引用到模块级常量）
```

### Problem → Fix

#### Problem 1：跨 3 文件常量重复定义（最关键）
- **发现**：grep 阶段扫描到 `REFUND_WINDOW_DAYS = 7` 在 `app/services/refund_graph.py:98` + `app/tools/refund_tool.py:21` 两处硬编码，`DELIVERY_OFFSET_DAYS = 2` 在 `refund_graph.py:100` + `order_lifecycle.py:36` 两处硬编码
- **决策**：按 CLAUDE.md §5.2 跨模块四要素（业务原因 / 接口变化 / 影响范围 / 隔离策略）一次迁移 3 文件
- **方案**：3 文件顶部统一 `_RULES = get_config_loader().load("refund")`，常量赋值保持原名（同名引用透明替换），保留调用方零改动

#### Problem 2：test 污染（最深坑）
- **现象**：单独跑 `test_refund_config.py` 9 个测试全过，全量 pytest 跑 6/9 失败（`ConfigNotFoundError: refund`）
- **根因二分定位**：`test_guard_config.py` 在 test_refund_config.py 前跑，其 fail-fast 测试 `TestGuardFailFast::test_missing_guard_yaml_raises_at_import` 通过 `monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", str(empty_dir))` 改 BUSINESS_RULES_DIR 后 `reload(app.services.guard)`
  - `get_config_loader()` 内部 `_loader = YAMLConfigLoader(base)` 赋值发生在 `load("guard")` **之前**
  - `load("guard")` 抛 ConfigNotFoundError 被 pytest.raises 接住
  - monkeypatch 撤销 BUSINESS_RULES_DIR，但 `_loader` 仍指向 monkeypatch 后的目录
  - 下个文件 `test_refund_config.py` 第一行 `get_config_loader().load("refund")` → 拿到污染的 loader → 失败
- **修复**：
  1. `test_guard_config.py` 加 post-only autouse fixture（文件末尾 reset `_loader = None`）
  2. `test_refund_config.py` 同款 autouse
  3. `test_guard_chitchat_is_same_object_as_yaml_dict` 的 `is` 断言改为 `==`（autouse 在测试间 reset cache → guard 模块顶层绑定的旧 dict 与新 load 出的 dict 不是同一对象）
- **最小修改**：只动 test 文件，不动生产代码（loader 工厂缓存语义改起来是更大动作，单独 PR）

#### Problem 3：`is` 断言脆弱（次坑）
- **原理**：pre+post autouse fixture 会清 cache → 同一模块两次 `load()` 返回不同 dict 对象（虽然内容相等）
- **决策**：保留生产代码"共享同一对象"的语义（业务模块顶部一次性赋值），但测试用 `==` 等值断言而非 `is` 同一对象断言
- **trade-off**：放弃"启动期 dict 复用"的优化断言（验证 hot-reload 准备的代码层），保留"内容一致"语义

### Architecture Role
属于 `services/` 层的基础设施 + `config/business_rules/` 配置层：
- **`config_loader.py`**：基础能力（CLAUDE.md §9.1 三大铁律 · Interface First），业务模块只能通过 `get_config_loader()` 工厂入口（禁止直接 `new` YAMLConfigLoader）
- **business_rules YAML**：配置与业务逻辑分离（CLAUDE.md §9.4.2），启动期一次加载，不参与热更新（roadmap §3.5 明确）
- **未来扩展位**：S6 多租户可在 loader 层加 tenant 维度（`load(name, tenant_id=None)`），MVP 阶段不实现

### 配套测试（9 用例）
- **TestRefundModuleLoadsYAML**（5）：YAML 含 2 字段 / 3 文件值与 YAML 一致 / 单一真相源（3 文件值必须全相等）
- **TestRefundPublicAPI**（3）：`refund_graph` / `RefundTool` / `order_lifecycle` 公共 API 兼容（保留类属性语法 + 模块级常量）
- **TestRefundFailFast**（1）：YAML 缺失 → import 阶段 ConfigError

### Phase 2 进度
S4 ✅ 3/4 阶段（config_loader + guard + refund）；阶段 4（intent.yaml + query_rewriter.yaml）+ 删除 legacy 薄壳待办。

### 已知限制
- 启动期一次加载，不参与热更新（roadmap §3.5 明确；如需热更 → 重启服务）
- name 仅单层 `[a-z0-9_]+`，不支持分层（与 prompt_loader 区别）
- MVP 无租户级覆盖（S6 范围）
- 无 Pydantic schema 校验（YAGNI：业务代码访问字段时自然抛 KeyError/TypeError）

### 反思（教训沉淀）
- **常量扫描先 grep 再迁移**：迁移前必须 grep 找出所有重复定义；3 文件共享常量是隐性技术债
- **monkeypatch + reload + factory singleton 是污染三件套**：测试隔离必须用 autouse fixture 强制重置工厂单例
- **`is` vs `==` 在 fixture 重置场景下的取舍**：测试要反映"生产实际能跑"，但不要 over-specify 生产未保证的优化（如 hot-reload 共享 dict 对象）

---

## 29. Sprint 4 业务规则 YAML 化 — 阶段 4：intent（2026-07-13）

### What
将 `intent_service.py` 的 81 条意图分类 pattern（4 类意图）+ 2 组实体抽取正则（订单号 / SKU），迁移到 `config/business_rules/intent.yaml`，启动期一次加载。commit `1338a09`。

### Why
CLAUDE.md §9.4.2「业务规则禁止硬编码」+ roadmap G8 缺口。意图 pattern 是典型业务规则，改一条关键词需改 Python 代码，应配置化。

### Tech Stack
- config_loader（阶段 1 基础设施）`.load("intent")`
- YAML dict + Python 3.7+ dict 保序 → 意图顺序敏感逻辑不变
- `frozenset(IntentType.__args__)` 校验 YAML key 合法（Literal 非 Enum）
- `getattr(re, flags_name)` 动态解析正则 flags

### Flow
`get_config_loader().load("intent")` → 校验 key ∈ IntentType → 构建 `INTENT_RULES: dict[str, list[str]]` + 编译 `ORDER_NO_RE` / `SKU_RE` → `classify()` 行为不变

### Problem → Fix
| 问题 | 根因 | 修复 |
|------|------|------|
| `IntentType(str)` 报 TypeError | `IntentType` 是 `Literal` 非 `Enum`，不可构造 | 改用 `frozenset(IntentType.__args__)` 做成员校验 |
| `for i, p in INTENT_RULES:` 解包失败 | 结构从 `list[tuple]` 改 `dict` | 改 `.items()` 迭代 |
| 测试 `flags == re.IGNORECASE` 失败（34≠2） | `re.compile` 后 flags 含 UNICODE 默认位 | 测试改按位与 `flags & re.IGNORECASE` |

### 配套测试（14 用例）
`test_intent_config.py`：YAML 字段/意图/pattern 计数、常量↔YAML 一一对应（防偏移）、顺序敏感、正则匹配、classify 3 类行为一致性、订单号抽取、fail-fast（YAML 缺失 → import 抛错）。全量 212/212 PASS，CI run #9 success。

### 已知限制
- `prompt_assembler._ORDER_NO_RE` 与 `intent.yaml` ORDER_NO_RE_PATTERN 语义相同但物理双源（M13 同步过）；本阶段不合并（YAGNI + 跨模块），后续 Sprint 可单独立项消除双源
- query_rewriter.py 业务规则仍待迁移（Phase 4 范围）

---

## 30. Sprint 4 业务规则 YAML 化 — 阶段 5 + 收尾（2026-07-14）

### What
闭环 Sprint 4「业务规则配置化 + 跨模块 Prompt 抽取」整条线：
1. **阶段 5**：query_rewriter 业务规则（20 代词 + 4 阈值）+ Prompt 抽取（system / user_template）
2. **收尾**：删 2 个 legacy 薄壳（`services/rerank.py` + `services/synthesizer.py`）；核心客户端（qwen.py / embedding.py）按用户决策保留为 Provider 内部实现，docstring 重写为"Provider 内部 DashScope 客户端"
3. 跨脚本迁移：`scripts/eval_hitk.py` + `scripts/gen_eval_set.py` 全部切到 Provider

### Why
- §9.4.2「业务规则禁止硬编码」+ §9.6「Prompt 独立管理」同步推进；query_rewriter 是 Sprint 3 留尾的"余 3 个跨模块 Prompt"之一
- 阶段 5 让 query_rewriter 服务（被 orchestrator 调，是 RAG 上游）对齐 G8 全员配置化基线
- 收尾闭合 Sprint 1 以来「qwen.py / embedding.py / rerank.py 薄壳 + synthesizer.py 薄壳」的悬挂清单，删薄壳、留核心（避免 Provider 无限套娃），符合 CLAUDE.md §3.3 YAGNI

### Tech Stack
- `app.core.retry_utils`（新增）— 抽 `_is_retryable` + `_calc_backoff` 出来作为 `app.core.qwen` 的依赖；qwen.py 改用 `from app.core.retry_utils import is_retryable as _is_retryable, calc_backoff as _calc_backoff` 保持向后兼容
- `app.core.providers.{llm,embedding,rerank}` 全部已存在（Sprint 1 落地）
- `app.services.config_loader` + `app.services.prompt_loader`（Sprint 4 阶段 1 落地）

### Flow

#### query_rewriter YAML 化
```python
# app.services.query_rewriter
_RULES = get_config_loader().load("query_rewriter")
_COREF_PATTERN = re.compile("|".join(re.escape(p) for p in _RULES["COREFERENCE_PATTERNS"]))
SYSTEM_PROMPT = get_prompt_loader().load("query_rewriter/system")
USER_TEMPLATE = get_prompt_loader().load("query_rewriter/user_template")
MAX_HISTORY_TURNS = _RULES["MAX_HISTORY_TURNS"]  # 4
MAX_HISTORY_MSG_LEN = _RULES["MAX_HISTORY_MSG_LEN"]  # 100
MAX_REWRITE_RATIO = _RULES["MAX_REWRITE_RATIO"]  # 3
MAX_REWRITE_EXTRA = _RULES["MAX_REWRITE_EXTRA"]  # 50
```

#### 收尾 — 删除 vs 保留决策

| 模块 | 决策 | 理由 |
|------|------|------|
| `app.services.rerank.py` | **删** | 12 行 wrapper，0 业务逻辑，全仓 0 引用 |
| `app.services.synthesizer.py` | **删** | 65 行 re-export（兜 Sprint 3 拆分前 import 路径），全仓 0 引用 |
| `app.core.qwen.py` | **保留** | Provider 内部 DashScope 客户端（保留 retry / 断路器 / 429 重试 / metrics 上报）；删除需重写 3 个 Provider 共 ~150 行 |
| `app.core.embedding.py` | **保留** | 同上；QwenEmbeddingProvider 委托之 |
| 3 个 Provider 文件 | **docstring 重写** | 去掉 "legacy" 字样；改为"内部委托给 Provider 内部 DashScope 客户端；业务模块禁止直接 import" |

### Problem → Fix

| 问题 | 根因 | 修复 |
|------|------|------|
| policy_service 切 Provider 后 3 测试 fail | tests 仍 mock `embed_text` / `rerank` 函数旧路径 | 改 mock `get_embedding_provider` / `get_rerank_provider` Provider 工厂入口，构造 `MagicMock(embed_text=MagicMock(return_value=...))` 链式 |
| 编辑器 Edit 太激进删 query_rewriter 顶部 + 改坏 logger | 单次 old_string 范围过宽 | 拆 2 次 Edit：先恢复 logger，再分块替换常量声明 |
| 想全删 qwen.py / embedding.py 触 ~150 行 Provider 重写 | Provider 内的 `_legacy_qwen.embed_text(...)` 委托依赖 | 用户决策保留 qwen.py / embedding.py；只改 docstring 表述（业务模块不易误用即可） |

### 验证
- pytest 全量 **224/224 PASS**（含新 12 个 `test_query_rewriter_config.py` 用例）
- grep 终验：
  ```bash
  grep -rn "from app.services.synthesizer\|from app.services.rerank" backend/  # ✅ 0 命中
  grep -rn "from app.core.qwen\|from app.core.embedding" backend/app/services/  # ✅ 0 命中（业务层走 Provider）
  ```
- `pytest --tb=short` 无 warning 升级

### Phase 4 / Phase 5 进度
- S4 ✅ 5/5 阶段 + 收尾（config_loader + guard + refund + intent + query_rewriter + 收尾）；Sprint 4 整条线 **100% 闭环**
- G8（业务规则 YAML 化）= 5/5（guard / refund / intent / query_rewriter / config_loader 架子）
- §9.6（Prompt 独立管理）= 5/5 YAML（refund / guard_chitchat / orchestrator / intent / query_rewriter）

### 已知限制
- qwen.py / embedding.py 是 Provider 内部客户端，物理存在但 docstring 已禁止业务 import；后续若写第二个 Provider（如 BGE）需要无痛替换即可触发真"删除"动作
- retry_utils 当前只被 qwen.py 引用，将来 Provider 跨模块重写时可下沉到 Provider 内部
- query_rewriter 的 prompt 提取只覆盖 system + user_template 两段，OpenAI 兼容模式下 messages 框架仍由 query_rewriter.py 写，未来若要支持 stream 改写需配套扩展

### 反思（教训沉淀）
- **Provider 抽象 vs 薄壳的边界模糊**：当 Provider 完全委托给 legacy 时，"删 legacy 重写 Provider" 和 "保留 legacy 改 docstring" 是 2 种合理路径；判断标准 = 当前是否真有第二个 Provider 要落地。本项目没有 → 选保留，docstring 表述 + grep 0 引用 = 双保险
- **测试 mock 跟随实现升级而漂移**：业务切 Provider 后，测试必须同步切 mock 入口；否则会因 patch 路径变成 no-op 而 hold 旧的真实代码路径（且 assert 失败方式不直观）。建议：Provider 切换时把测试 mock 修复列入 commit checklist
- **彻底清退要含 scripts**：CLAUDE.md §7 分层里 `scripts/` 不在分层图里，但它会绕过 Provider 直接调旧函数；Sprint 收尾必须 `scripts/` 一起切，否则下次重启就是定时炸弹
- **§3.3 YAGNI vs 持有成本的真实权衡**：删 12 行薄壳 + 65 行 re-export 是单方向受益（少维护、grep 0 命中）的，不删的 2 个 Provider 内部客户端是要权衡「重写 Provider 引入 regression 风险」的 — 用户协作风格刚好契合：先列出方案差异，再选保守方向

---

## 31. Phase 4 A4 — query_rewriter 多路改写 + policy 多路 RRF 融合（2026-07-14）

### What
在 Sprint 4（业务规则 YAML 化 + Prompt 抽取）闭环基础上，给 `query_rewriter.py` 加 **Multi-Query 业务能力增强**：
1. 新增 `rewrite_query_multi(query, history, n)`：从 1 路改写升级为 N 路改写（默认 N=3），沿用 L0/L1/L2 防浪费链路 + 多路降级兜底
2. 新增 `PolicyService.search_multi_policy(queries, top_k)`：多路 query → 每路独立 RAG → RRF（Reciprocal Rank Fusion）融合 → top_k
3. `chat/orchestrator.py` 调度：`ENABLE_MULTI_QUERY` 灰度开关 + `search_queries is None` 走单路（mock 兼容保留）
4. `scripts/eval_hitk.py` 加 `--multi-query` 评估开关，hit@K 报告可量化 Multi-Query 提升
5. 新增 2 Prompt YAML（`multi_system` + `multi_user_template`），与 query_rewriter.yaml 同目录管理
6. 配套 18 用例（`test_query_rewriter_multi.py` 12 + `test_policy_service_multi.py` 6）+ 1 YAML 配置用例 + eval_hitk 接入

### Why
CLAUDE.md §9.4.2 业务规则 + §9.6 Prompt 已配置化完毕；query_rewriter 服务已具备 LLM 调用 + JSON 解析 + 多路兜底基础设施，**业务能力纵深**成为下一价值点。
- **单路改写盲点**：含指代词的 query 即使改写成功，召回仍然受限于 1 路 embedding；同义改写（如"它能退吗" → ["退货流程", "如何申请退款", "退货运费险"]）能扩召回覆盖
- **多路 RRF 融合成熟**：Cormack 2009 RRF（k=60）已在 `app.services.rrf` 落地，BM25 + dense 双向融合已用；扩展到 N 路 query 等价复用
- **灰度开关**：Sprint 4 阶段 5 已落地配置化，加 1 行 `ENABLE_MULTI_QUERY: bool = False` 即获得零侵入灰度能力
- **CLAUDE.md §3.3 YAGNI**：只做 Multi-Query，**不**做 HyDE / 同义词 / 改写模型微调（这些是 V3+ 业务能力纵深）

### Tech Stack
- `app.services.query_rewriter`（已有）— 加 `rewrite_query_multi` 函数 + `MULTI_SYSTEM_PROMPT_TEMPLATE` / `MULTI_USER_TEMPLATE` 常量（启动期 prompt_loader 加载）
- `app.services.policy_service`（已有）— 加 `search_multi_policy` 静态方法（本地 import rrf_fuse 防循环）
- `app.services.chat.orchestrator`（已有）— `run_stream` 加 `search_queries` 中间变量，按 intent 分派时透传
- `config/prompts/query_rewriter/{multi_system, multi_user_template}.yaml`（新增）
- `config/business_rules/query_rewriter.yaml`（已有 · 追加 3 字段：ENABLE_MULTI_QUERY / MULTI_QUERY_COUNT / MULTI_QUERY_TRIGGER）
- `core/config.py`（已有 · 追加 3 个 Pydantic settings）
- `services/metrics.py`（已有 · 加 `inc_rewrite_multi(reason)` 计数器 + snapshot 暴露 `rewrite_multi_block`）
- `tests/test_query_rewriter_multi.py`（新增 · 12 用例）
- `tests/test_policy_service_multi.py`（新增 · 6 用例）
- `scripts/eval_hitk.py`（已有 · 加 `--multi-query` flag）

### Flow

#### 改写侧：rewrite_query_multi 防浪费链路
```
query + history
   │
   ├─ L0: 无指代词? ── 是 ──> ([query], was_rewritten=False)    # 0 LLM token
   │
   ├─ L1: 无 history? ── 是 ──> ([query], was_rewritten=False)  # 0 LLM token
   │
   └─ L2: LLM call
         │
         ├─ JSON parse fail ──> ([query], was_rewritten=False, reason=parse_fail)
         │
         ├─ 有效变体 < 2 ──> ([query], was_rewritten=False, reason=too_few_variants)
         │
         ├─ 变体超过 MAX_RATIO*orig + MAX_EXTRA ──> drop（不计入）
         │
         ├─ 变体 == 原 query ──> exclude（防伪变体）
         │
         ├─ 变体重复 ──> dedup
         │
         └─ 有效变体 ≥ 2 < N ──> pad with orig to N → ([v1, v2, orig], was_rewritten=True)
```

#### 检索侧：search_multi_policy 多路 RRF
```
queries = [q1, q2, q3]
   │
   ├─ queries 为空? ── 是 ──> []   # 短路
   │
   ├─ queries 长度 = 1? ── 是 ──> [PolicyService.search_policy(q1, top_k)]  # 短路
   │
   └─ 多路：每路 search_policy
         │
         ├─ 单路异常? ── 是 ──> skip 该路，continue
         │
         ├─ 全路失败? ── 是 ──> []
         │
         └─ rrf_fuse(per_query_hits, k=60) → top_k
               │
               └─ RRF 异常? ──> 降级到首路前 top_k
```

#### orchestrator 集成
```python
# orchestrator.run_stream
rewritten_query, was_rewritten = rewrite_query(query, history)         # M12：单路改写
query = rewritten_query

# Phase 4 A4：Multi-Query（仅 policy/product 有效；ENABLE_MULTI_QUERY 默认 false）
search_queries = None
if settings.ENABLE_MULTI_QUERY:
    multi_queries, _ = rewrite_query_multi(query, history)
    if multi_queries and len(multi_queries) > 1:
        search_queries = multi_queries

# 分派：search_queries is None → 单路（mock 兼容）；非 None → 多路
if intent == "product_query":
    yield from Synthesizer._handle_product(..., search_queries=search_queries)
elif intent == "policy_query":
    yield from Synthesizer._handle_policy(..., search_queries=search_queries)
```

```python
# _handle_product / _handle_policy 内部
if search_queries:
    kb_docs = PolicyService.search_multi_policy(search_queries, top_k=3)
else:
    kb_docs = PolicyService.search_policy(query, top_k=3)  # mock 兼容
```

### Problem → Fix

| 问题 | 根因 | 修复 |
|------|------|------|
| pytest 报 `test_anti_hallucination` 失败（PolicyService mock 路径没命中） | orchestrator 无条件调 `search_multi_policy` 绕过了测试 patch 的 `search_policy` | orchestrator 改为 `if search_queries` 条件调度：`search_queries is None` 走单路 `search_policy`（mock 兼容）；非 None 走多路（生产路径） |
| pytest 新文件 `ValueError: JWT_SECRET must be set` | env vars 写在 `if __name__ == "__main__":` 块底，pytest 不走 `__main__` | 移到模块顶部 `os.environ.setdefault(...)`（与 Sprint 4 阶段 5 测试同模式） |
| RRF mock patch 路径错（`patch("app.services.policy_service.rrf_fuse")` no-op） | `rrf_fuse` 是本地 import `from app.services.rrf import rrf_fuse` | mock 用模块实际名字空间 `patch("app.services.rrf.rrf_fuse")` |
| Multi-Query 误扩到 order/refund 路径 | orchestrator 无差别分派可能影响 order/refund 业务 | 只在 `product_query` / `policy_query` 透传 `search_queries`；order/refund 不变（YAGNI + 业务无 RAG 召回需求） |
| 变体 == 原 query 干扰 RRF | LLM 可能回退到原 query（保守改写） | 在 dedup 前 `if v == orig: continue` 排除；有效变体不足时再 pad |
| 变体过长被 LLM 注入噪音 | LLM 可能返"详细退货指南（含完整退款流程 + 时效 + 注意事项）"远超 query 长度 | 长度上限 `len(orig) * MAX_REWRITE_RATIO + MAX_REWRITE_EXTRA`（= 4*3+50=62），超长 drop |
| 反幻觉：LLM 改写编造订单号 | 多路改写可能扩散 LLM 幻觉到多路召回 | system prompt 加"不要编造订单号 / SKU / 数字"硬约束（与 query_rewriter 单路 prompt 同策略） |

### 验证
- pytest 全量 **243/243 PASS**（224 baseline + 12 query_rewriter_multi + 6 policy_service_multi + 1 YAML 配置测试）
- 新增 2 Prompt YAML 通过 `prompt_loader.load("query_rewriter/multi_system")` / `multi_user_template` 验证（启动期 fail-fast）
- `ENABLE_MULTI_QUERY=False` 灰度基线：单路路径行为与 Sprint 4 闭环完全一致（grep 0 业务路径变化）
- eval_hitk.py `--multi-query` 接入完成，待真实流量验证 hit@K 提升幅度
- CI 状态：GitHub Actions 等待 commit 2 push 后 run

### Architecture Role
属于 `services/` 层的**业务能力纵深**模块，定位为 Sprint 4 配置化之上的"能力层"：
- **上游**：Sprint 4 已落地 config_loader + prompt_loader（基础）+ query_rewriter.yaml（业务规则）
- **下游**：`policy_service.search_multi_policy` 是 `policy_service.search_policy` 的**多路超集**；orchestrator 调度逻辑不变（仅多 1 个 search_queries 中间变量）
- **依赖方向**：`query_rewriter` → `prompt_loader` / `config_loader` / `llm_provider`（单向）；`policy_service.search_multi_policy` → `policy_service.search_policy` + `rrf`（单向复用）
- **CLAUDE.md §9.1 三大铁律**：
  - Interface First：rewrite_query_multi 走 `LLMProvider` 抽象（不在 query_rewriter 内 import qwen.py）；search_multi_policy 走 `EmbeddingProvider` + `RerankProvider`
  - Module Isolation：query_rewriter 不感知 policy_service；policy_service 不感知 orchestrator；orchestrator 是唯一串联点
  - Dependency Inversion：业务模块不直接 `from app.services.policy_service import search_multi_policy`（除 orchestrator），均通过 `PolicyService.search_multi_policy(...)` 静态调用

### 配套测试（18 用例）

**`tests/test_query_rewriter_multi.py`（12）**
- L0 短路：无指代词 → `([query], was_rewritten=False)`，不调 LLM（mock.assert_not_called）
- L1 短路：无 history → 同上
- L2 成功：LLM 返 3 条 JSON → `[v1, v2, v3]` + `was_rewritten=True`
- LLM message 格式：`multi_system` + `multi_user_template` 模板拼接正确，含 `{n}` 占位
- parse_fail：LLM 返非 JSON → `([query], was_rewritten=False)`
- too_few_variants：LLM 仅返 1 条 → 降级
- llm_error：LLM 异常 → 降级
- 长度超限过滤：变体超过 `orig*MAX_REWRITE_RATIO + MAX_EXTRA` → drop
- 去重 + 填充：重复变体 dedup；不足 N 条用 orig 填充
- == orig 排除：变体 == query → 不计入
- YAML 字段加载：`ENABLE_MULTI_QUERY` / `MULTI_QUERY_COUNT` / `MULTI_QUERY_TRIGGER`
- 多 Prompt 加载：`MULTI_SYSTEM_PROMPT_TEMPLATE` / `MULTI_USER_TEMPLATE` 是 str 且含 `{n}` / `{history}` / `{query}` 占位

**`tests/test_policy_service_multi.py`（6）**
- 空 queries：返 `[]`
- 单 queries：短路返 `search_policy` 结果（不调 RRF）
- 多 queries 正常：3 路 RAG + RRF 融合，输出含 `rrf_score` 字段
- 单路异常：仅该路降级，其他路继续
- RRF 异常：降级到首路前 top_k
- schema 一致：与 `search_policy` 字段对齐（text/source/score/rerank_score/rrf_score）

### 阶段进度
- **Phase 4 A4 ✅ 完成**：1 feature commit（`333e01b`）+ 1 test+docs+eval commit（待 push）
- **§9.4.2 业务规则配置化**：5/5 服务（guard / refund / intent / query_rewriter / config_loader）
- **§9.6 Prompt 独立管理**：7/7 YAML（refund / guard_chitchat / orchestrator / agent / no_login / intent / query_rewriter + 新增 2 multi_*）

### 已知限制
- `ENABLE_MULTI_QUERY=False` 默认关闭，需手动 Pydantic settings 打开才能灰度验证（无运行时开关）
- 多路 query 实际响应延迟 ≈ 单路 × N（同步串行）；N=3 时 p50 翻 3 倍，需配合 SSE 流式增量返回优化（V3+）
- 多路融合仅走 RRF，未做加权（无业务层对不同 query 变体的可信度打分）
- Rerank 在多路融合**前**调用（每路独立 rerank），未在融合**后**统一 rerank；理论上前者更稳定（候选级精度高），后者更聚合（全局重排），当前选前者（工程复杂度低）
- HyDE / 同义词扩展未做（CLAUDE.md §3.3 YAGNI）
- 变体数 N 写死 3（YAML 可调但未跑过 N=4 / N=5 的 eval 对比）

### 反思（教训沉淀）
- **「多路 vs 单路」的接口兼容模式**：orchestrator 不能无条件切到新接口，否则会绕过测试 mock；`search_queries is None` 单路 + 非 None 多路 这种"中间变量"模式让 mock 测试零修改兼容，是与 Sprint 3 `_handle_product` / `_handle_policy` 内部重构同款的「灰度切换」范式
- **防浪费链路 L0/L1/L2 的可复用性**：query_rewriter 单路已落地 L0（无指代词）+ L1（无 history）+ L2（LLM call），多路直接复用同一套判定 + 加一段 LLM 输出校验（JSON / dedup / length / pad），证明防浪费设计在能力扩展时不会变成阻力
- **本地 import 防循环依赖**：`search_multi_policy` 用 `from app.services.rrf import rrf_fuse`（函数内 import）而非模块顶部 import，避免 `policy_service ↔ rrf` 隐式循环（虽然在当前依赖图里也合法）
- **eval_hitk 接入是验证标准**：`--multi-query` flag 让 hit@K 报告可量化 Multi-Query 提升幅度，避免「能力上线了但效果未验证」的常见 Sprint 烂尾；同模式可以套到未来 HyDE / 同义词扩展

---

## 32. P2 长程记忆 — user_profiles + profile_service + prompt 注入（2026-07-14）

### What
P2 backlog 第 2 项落地：在 Sprint 4 + Phase 4 A4 闭环基础上，给客服系统加**跨 session 用户画像**：
1. 新增 `user_profiles` 表（1:1 → users.id）存 summary / frequent_skus / preferences / interaction_count
2. 新增 `services/profile_service.py`（get_or_create / update_summary / append_frequent_skus / increment_interaction / clear / to_prompt_block）
3. `chat/orchestrator.py` 启动期加载 profile → 转 `profile_block` → 注入 `context_block` 末尾
4. `api/chat.py` done 事件后 best-effort 累加 `interaction_count` + 追加 `frequent_skus`
5. 灰度开关 `settings.ENABLE_USER_PROFILE=False` 默认关闭（与 Phase 4 A4 同模式）
6. 27 用例 `tests/test_profile_service.py` 全 mock 测（with_safe_session / UserProfile / 异常路径）

### Why
CLAUDE.md §9.5.2 可观测要求「单用户级分析」+ 真实场景中用户**跨 session 重复问同一类问题**（如"运费险怎么买"问 3 次），AI 没有长程记忆就只能每次从零答。
- **业务价值**：让 AI 记住"这位用户上次问过什么、关心什么商品、有什么偏好"，给个性化回复
- **基础设施就绪**：Sprint 4 已闭环配置层（config_loader + prompt_loader），profile_service 直接复用 `with_safe_session` best-effort 模式
- **§3.3 YAGNI**：只做"profile 加载 + 自动更新"，不做事件流 / 画像聚类 / 租户级

### Tech Stack
- `app.models.user_profile.UserProfile`（新增）— ORM 1:1 users.id
- `app.services.profile_service`（新增）— 5 个写入函数 + 1 个格式化函数（纯函数）
- `deploy.mysql.init.02_user_profiles.sql`（新增）— Docker init 脚本（与 01_schema.sql 同模式：DROP IF EXISTS + CREATE）
- `app.core.config.settings`（已有）— +2 字段（ENABLE_USER_PROFILE / USER_PROFILE_PROMPT_MAX_LEN）
- `app.services.chat.prompt_assembler._build_context_block`（已有）— 扩 `profile_block` 参数
- `app.services.chat.orchestrator.run_stream`（已有）— 启动期 `profile_service.get_or_create` → `to_prompt_block` → 拼 context
- `app.api.chat.event_generator`（已有）— done 事件后 `increment_interaction` + `append_frequent_skus`

### Flow

#### 注入侧：每轮 /chat 加载 profile → 注入 context
```
user_id = 1, ENABLE_USER_PROFILE = True
   │
   ├─ run_stream 启动期
   │   profile = profile_service.get_or_create(1)        # 不存在则建空
   │   profile_block = profile_service.to_prompt_block(profile, max_len=200)
   │
   ├─ context_block = _build_context_block(
   │     sku, order_no, user_id, profile_block=profile_block
   │   )
   │   # M9.5 context（商品/订单）优先；profile_block 拼末尾（补充信息）
   │
   └─ LLM prompt = 【当前场景】M9.5 + 【当前用户画像】profile + 【对话历史】 + 问题
```

#### 更新侧：每轮 done 后 best-effort 累加
```
done 事件触发
   │
   ├─ ENABLE_USER_PROFILE and user_id != 0?
   │   ├─ 否 → 短路（不放行 profile 调用）
   │   └─ 是 → asyncio.to_thread(profile_service.increment_interaction, user_id, 1)
   │              # 每轮 +1
   │              if payload.sku:
   │                  asyncio.to_thread(profile_service.append_frequent_skus,
   │                                      user_id, [payload.sku])
   │
   └─ 异常 → warning + 放行（不影响 done 响应）
```

#### to_prompt_block 格式化（核心纯函数）
```python
profile = UserProfile(
    summary="用户近期关注 ZP1 配件",
    frequent_skus=["ZP1", "ZP2"],
    preferences={"refund_pref": "fast"},
    interaction_count=12,
)
block = to_prompt_block(profile)
# → "【当前用户画像】(跨 session 长程记忆，仅作参考，不得编造...)
#    - 偏好：refund_pref=fast
#    - 最近提过的商品：ZP1 / ZP2
#    - 画像摘要：用户近期关注 ZP1 配件
#    - 累计对话：12 轮"
# 硬上限 max_len=200（防 prompt 膨胀）
```

### Problem → Fix

| 问题 | 根因 | 修复 |
|------|------|------|
| 测试 mock `with_safe_session` 报 "missing positional argument db" | `_patch_safe_session(db, commit=True)` 签名不匹配：实际是 `with_safe_session(commit=True) as db:` 调用，`db` 由 `__enter__` yield 出来 | 改 `_patch_safe_session_with_db(db)` 工厂，闭包捕获 db；mock 接受 `*, commit=True` |
| `to_prompt_block` 硬截断没生效（104 字 > max_len=50） | 原实现先 truncate `body`，再拼 prefix label，导致最终 block 超出 max_len | 改：先拼 prefix + body 得到完整 block，再对 block 整体截断 |
| 真 DB 还没建 user_profiles 表（deploy init 在 fresh DB 才跑） | 当前 dev DB 是手工 migrate 上来；新增表需要新建部署 | `deploy/mysql/init/02_user_profiles.sql` 已就位，新部署自动建；dev DB 需要手工跑或重启容器 |
| profile 加载失败导致主流程卡死 | profile_service 内部异常未捕获 | profile_service.get_or_create 内部 `try/except + warning + return None`；orchestrator 外层再 try/except 一次（双保险） |
| 隐私删除链路缺失 | 用户有权清除画像但无入口 | profile_service.clear() 软删接口就位；admin API / 用户自助入口留 V3+ |
| 反幻觉：LLM 把 profile 内容当真 | profile 是历史摘要，LLM 可能当事实引用 | to_prompt_block 输出带 hard label"仅作参考，不得编造未在 profile 中出现的用户事实"（与 M9.5 同模式） |
| 灰度开关默认开导致 0 LLM token 失控 | profile 注入增加 prompt tokens（最多 200 字） | `ENABLE_USER_PROFILE=False` 默认关闭；先观察真实效果再开 |
| YAGNI 越界风险：想加"画像聚类 / 事件流 / 租户级" | 当前 1 表 + 1 service 边界清晰 | §3.3 决策表：仅做"1:1 画像 + best-effort 自动更新"，不做事件流（messages JOIN）/ 派生画像（summary 够用）/ 租户级（profile 跟 user 走） |

### 验证
- pytest 全量 **270/270 PASS**（243 baseline + 27 profile_service）
- `ENABLE_USER_PROFILE=False` 灰度基线：现有测试零修改通过（profile_block 始终空串，context_block 行为不变）
- 新增 27 用例覆盖：get_or_create / update_summary / append_frequent_skus（去重+截断）/ increment_interaction / clear（软删）/ to_prompt_block（结构化+硬截断+反幻觉 label）/ 隐私边界（user_id=0 短路）/ 灰度开关
- `with_safe_session` mock 模式：与 Sprint 4 refund/guard/intent/query_rewriter 测试同模式（autouse fixture 不需要，因 service 函数本身可幂等）

### Architecture Role
属于 `services/` 层的**业务能力纵深** + 跨模块编排（orchestrator / api/chat / prompt_assembler / profile_service / user_profile ORM）：
- **上游**：Sprint 1-4 已落地 Provider + config_loader + prompt_loader + audit_service（best-effort 模式参考）
- **下游**：`chat/orchestrator` 是唯一串联点（profile 加载 → 注入 context → done 后更新）
- **依赖方向**：`profile_service` → `models/user_profile` + `clients/mysql_client`（单向）；`chat/orchestrator` → `profile_service`（单向）
- **CLAUDE.md §9.1 三大铁律**：
  - Interface First：profile_service 是 5 个**函数**（非类），与 session_service / order_service / policy_service 同模式（业务模块 1 个实现未抽 Protocol，符合 §3.3 YAGNI）
  - Module Isolation：profile_service 不感知 chat/orchestrator；orchestrator 不感知 UserProfile ORM；api/chat 仅在 done 事件后调 profile_service
  - Dependency Inversion：业务模块只 `from app.services import profile_service`（工厂模式），不直接 new ORM

### 配套测试（27 用例）

**`tests/test_profile_service.py`（27）**
- `TestGetOrCreate`（4）：user_id=0 短路 / 行存在返 / 行不存在建空 / DB 异常 None
- `TestUpdateSummary`（4）：user_id=0 / 行存在更新 / 行不存在插入 / DB 异常 False
- `TestAppendFrequentSkus`（4）：空 list 短路 / 去重+截断 / 行不存在插入 / DB 异常 False
- `TestIncrementInteraction`（3）：行存在累加 / 行不存在建 / user_id=0 False
- `TestClear`（3）：行存在软删 / 行不存在 False / user_id=0 False
- `TestToPromptBlock`（7）：None 返空 / 全空返空 / 结构化输出 / interaction<3 隐藏 / 硬截断 / preferences 最多 3 / SKUs 最多 5
- `TestPrivacyBoundary`（1）：匿名 user_id=0 全部写路径短路 + 零 DB 调用
- `TestGrayscaleSwitch`（1）：to_prompt_block 不读 settings（开关由 orchestrator 把控）

### 阶段进度
- **P2 长程记忆 ✅ 完成**：1 feature commit（`37614e5`）+ 1 test+docs commit（待 push）
- **P2 backlog**：5/5 → 1/5 完成（CI 配置增强 / SSE resume / Prompt 版本 / HTTPS 待启动）
- **§9.5 安全可观测**（架构验收维度）：从"🟡 部分"升级中（profile 给"用户级分析"留基础设施）

### 已知限制
- profile 自动摘要仍是手动触发（done 后仅累加 interaction + SKUs，不调 LLM 摘要 summary）；summary 字段靠人工编辑 / 后续 LLM 摘要脚本补
- 跨租户场景（tenant_id 字段）未支持（与 Sprint 6 多租户 MVP 同步；当前 dev 单租户）
- 用户隐私删除缺 UI/admin API 入口（仅 profile_service.clear() 函数接口）
- interaction_count 不区分用户消息 / 助手消息（每轮 +1）；如要精确需读 messages.role
- profile_block 硬截断到 200 字 → summary 字段越长，截断后越模糊；后续 LLM 摘要脚本要控 summary 长度 ≤120 字

### 反思（教训沉淀）
- **mock 闭包捕获 vs 局部 mock**：`_patch_safe_session(db)` 第一版签名错了，因为 `with_safe_session(commit=True)` 调用方没传 db——是 `__enter__` yield 出来的。正确模式是 mock 工厂闭包捕获 db（`@contextmanager` + 闭包变量）
- **truncate 时机要正确**：「先 truncate 子内容再拼 prefix」与「先拼 prefix 再 truncate 整体」结果可能不同；硬上限应统一对最终 block 整体截断，避免遗漏 prefix 长度
- **灰度开关的位置**：to_prompt_block 是纯函数不应感知 ENABLE_USER_PROFILE（开关语义应在外层 caller 把控），这样 unit test 不需要 mock settings；orchestrator 是把控开关的正确层级
- **best-effort 双保险**：profile_service 内部 try/except + orchestrator 外层 try/except 双重防御；任何一层失效都不会阻塞主流程
- **隐私边界前置**：user_id=0（匿名）短路写在所有 5 个写函数里（不是顶层 if 判断），确保未来加新写函数时也能强制约束
- **YAGNI 决策表的实际收益**：当想"加画像聚类 / 事件流"时，§3.3 决策表 + 强制写进 commit message 是有用的刹车（commit 1 已注明"YAGNI 边界：1 张表 + 1 个 service；不做事件流 / 派生画像 / 租户级"）

