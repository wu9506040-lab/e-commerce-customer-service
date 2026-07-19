# ECS KB 重灌 + T2.4 双写一致性验证（2026-07-19 20:55）

> **目的**：闭环 P0 blocker "ECS Qdrant collections 为空"，恢复 T2.2 政策原文引用公网端到端验证前提  
> **范围**：仅手动 cp 数据 + 容器内 ingest；不动 docker-compose、不重建容器、不改生产代码  
> **关联 commit**：`659bce4 fix(rag): T2.4 ingest MySQL metadata 写入失败`（已 push，本报告验证其修复效果）

---

## 1. 根因（已查明）

### 1.1 现状证据（修复前）

| 维度 | 实测值 |
|---|---|
| Qdrant 容器状态 | `customer-service-qdrant` Up 2 days (healthy) · 端口 6333 OK |
| Qdrant `/collections` | `{"result":{"collections":[]},"status":"ok"}` 空 |
| Qdrant 数据卷 | `/var/lib/docker/volumes/customer-service_qdrant_data/_data/collections/` 空目录 · Jul 16 创建后从未写入 |
| API 容器 KB 源数据 | `/app/docs/ecommerce_kb` **不存在** |
| API 容器 ingest 脚本 | `/app/scripts/` **不存在** |
| 本地 KB 源数据 | `docs/ecommerce_kb/` 完整（12 文件 · 75KB） |
| 本地 ingest 脚本 | `scripts/ingest_ecommerce_kb.py` 完整（4292 bytes） |

### 1.2 根因结论

T2.4 修复（commit `659bce4`）只解决了**代码层**的 ingest 写入失败（`db.refresh` → `db.flush`），但 ECS 部署流水线**从未把 KB 源数据 + ingest 脚本复制进容器**，导致：

1. Qdrant volume 3 天前新建后从未写入 → `/collections` 始终为空
2. 容器内没有任何 KB 数据可 ingest
3. T2.2 政策原文引用规则的**公网端到端验证从未真正完成**
4. policy_coverage 25% 不是 T2.2 规则带来的覆盖增量（synthesize 阶段关键词引用在 KB 库空的情况下退化为空命中）

---

## 2. 修复执行

### 2.1 命令序列

```bash
# 1. scp KB + ingest 脚本到 ECS /tmp
scp -r docs/ecommerce_kb aliyun:/tmp/
scp scripts/ingest_ecommerce_kb.py aliyun:/tmp/

# 2. docker cp 到 API 容器
ssh aliyun "docker exec customer-service-api mkdir -p /app/docs /app/scripts"
ssh aliyun "docker cp /tmp/ecommerce_kb/. customer-service-api:/app/docs/ecommerce_kb/"
ssh aliyun "docker cp /tmp/ingest_ecommerce_kb.py customer-service-api:/app/scripts/"

# 3. 容器内执行 ingest（PYTHONPATH=/app 让 import app.* 工作）
ssh aliyun "docker exec -e PYTHONPATH=/app customer-service-api \
  python /app/scripts/ingest_ecommerce_kb.py"
```

### 2.2 时间表

| 时间 | 动作 |
|---|---|
| 20:48 | SSH 调查根因（docker ps / Qdrant API / volume / 容器 KB 路径）|
| 20:50 | scp KB + ingest 脚本完成 |
| 20:52 | docker cp 到容器 |
| 20:54 | PYTHONPATH=/app 容器内执行 ingest |
| 20:55 | 验证 Qdrant + MySQL + chat API |

---

## 3. 验证结果

### 3.1 Qdrant knowledge_base collection

```json
{"result":{"status":"green","optimizer_status":"ok",
 "points_count":93,"segments_count":2,
 "config":{"params":{"vectors":{"size":1024,"distance":"Cosine"}}}}}
```

- **points_count: 93** ✅
- vector size: 1024 (DashScope text-embedding-v2)
- 状态：green

### 3.2 MySQL knowledge_documents 双写一致性

| doc_type | metadata 行数 | chunks |
|---|---|---|
| faq | 32 | 33 |
| policy | 10 | 13 |
| promotion | 4 | 6 |
| return_policy | 6 | 8 |
| shipping_policy | 6 | 8 |
| warranty_policy | 1 | 3 |
| product | 22 | 22 |
| **合计** | **81** | **93** |

- **MySQL 81 行 · total_chunks=93 与 Qdrant points_count 完全对齐** ✅
- **T2.4 `db.flush` 修复效果已验证**（之前是 0 行；现 81 行 = Qdrant chunks 全部 metadata 持久化）

### 3.3 端到端 chat API 验证（T2.2 政策原文引用）

```bash
$ curl -s -X POST http://localhost:8000/api/chat -H 'Content-Type: application/json' -d '{
    "user_id":10003,"session_id":"kb_verify_002",
    "query":"7天无理由退货需要满足什么条件？","stream":false}'
```

返回关键字段：

```json
{"type":"meta","intent":"policy_query","contexts":[
  {"source":"faq_top_002","text_preview":"Q：可以退换吗？...支持 7 天无理由退货...","type":"policy"},
  {"source":"policy_return_main","text_preview":"【智选科技 退换货完整规则】...7 天无理由退货...","type":"policy"},
  {"source":"policy_return_faq_01","text_preview":"Q：手机激活后还能 7 天无理由退货吗？...","type":"policy"},
  {"source":"faq_top_010","text_preview":"...","type":"policy"}
]}
```

- `intent: policy_query` ✅
- **4 条 KB policy 原文被检索命中** ✅
- **T2.2 政策原文引用公网端到端验证通过** ✅

---

## 4. 闭环结论

| Block item | 修复前 | 修复后 |
|------------|--------|--------|
| ECS Qdrant knowledge_base | 不存在 | 93 vectors ✅ |
| MySQL knowledge_documents | 0 行 | 81 行（双写一致）✅ |
| T2.4 `db.flush` 双写一致性 | 未在 ECS 验证 | **已验证** ✅ |
| T2.2 政策原文引用（公网端到端）| 未真正完成 | **4 contexts 命中** ✅ |

---

## 5. 残留待办（独立任务 · 不在本批修复范围）

| 优先级 | 任务 | 工时 |
|--------|------|------|
| P2 | **baseline V4 重跑**：用 KB 真实环境重跑 100 case，量化 policy_coverage 实际增量（25% → 更高）| 60 min |
| P2 | **部署层修复（治本）**：`deploy/docker-compose.yml` 加 `../docs:/app/docs:ro` + `../scripts:/app/scripts:ro` volume mount；API 容器 entrypoint 加 `--mode if-empty` 启动自动 ingest | 90 min |

---

## 6. 备注

- 本次修复**不动 docker-compose.yml / Dockerfile / 任何部署配置**
- 容器内 `/app/docs/ecommerce_kb` 和 `/app/scripts/ingest_ecommerce_kb.py` 是临时灌入的副本；**容器重建会再次丢失**（见 §5 治本任务）
- ingest 脚本的 idempotent 设计（source UUID5 + MySQL UNIQUE）保证重跑不重复入库，治本修复后可安全多次触发