# RAG 数据中台架构（V18，2026-10-17）

## 设计原则

多渠道（闲鱼 / 商城）共用一套 Qdrant collection + MySQL，靠 **payload/会话前缀 隔离**，不建重复库；新数据走统一入库流水线（增量 + 分块 + 向量化），幂等可重跑。

## 1. Qdrant 渠道隔离

```
collection: knowledge_base（唯一）
point payload = {
  channel: "xianyu" | "mall",     # 渠道标签（检索过滤键）
  category, doc_type, source,     # 来源三元组
  title, text, chunk_index,
  content_hash                    # 增量去重键
}
```

- **写入**：`scripts/ingest_kb.py` 自动打标（文件名/category 含 xianyu → 闲鱼）
- **读取**：`qdrant.search(channel=...)` → Qdrant filter `payload.channel` 精确匹配
- **入口**：`policy_service.set_kb_channel("xianyu")`（contextvar，asyncio.to_thread 透传，每请求独立）——orchestrator.run_stream 在 channel=xianyu 时设置
- **验证**（2026-10-17）："能带上飞机吗" → xianyu 过滤命中 PB1 FAQ(0.73)；mall 过滤为空 ✅

## 2. 入库流水线（scripts/ingest_kb.py）

```
docs/ecommerce_kb/*.json
  → 增量判断（.ingest_state.json 记 source→content_hash，未变更零成本跳过）
  → 分块（>600 字按 "\n\n" 段落切，块间 100 字重叠）
  → 批量向量化（text-embedding-v3，10 条/批）
  → Qdrant upsert（带 channel）
  → MySQL knowledge_documents 镜像（source 唯一键 upsert，channel 列自动补）
```

幂等：重复跑只处理新增/变更；upsert 失败不清 state（下次重灌）。
用法：`python scripts/ingest_kb.py [--file xx.json] [--force]`

## 3. MySQL 会话隔离

| 层 | 机制 |
|----|------|
| 写入 | 闲鱼会话 session_id 固定前缀 `xianyu-{cid}`（reply_scheduler 生成） |
| 物理表 | messages/conversations 共表（数据量 3.5k，分表收益低） |
| 查询隔离 | 视图：`v_messages_xianyu` / `v_messages_mall` / `v_conversations_*`（session_id LIKE 'xianyu-%' 切分） |
| 业务表 | `xianyu_orders` 独立表（闲鱼专属）；商城 orders/products 不与闲鱼交叉 |

## 4. 检索路由总览

```
channel=xianyu 请求：
  ├─ 意图=chitchat → 免 LLM 短路
  ├─ product_query 等常规 → 快速通道（system_prompt 直出 LLM，不走 RAG）
  ├─ refund_query/order_query → 专用分支（查 xianyu_orders）
  └─ fallback 降级 RAG → set_kb_channel("xianyu") → 只检索闲鱼 KB
channel=None（商城 web）→ 原全管线（rewrite/multi/RAG），不过滤
```

## 5. 新增渠道/新数据 SOP

1. 新渠道：确定 channel 标识 → KB 文件命名带渠道关键词（自动打标）→ ingest 入库 → orchestrator 快速通道/fallback 接 set_kb_channel
2. 新数据：JSON 丢进 docs/ecommerce_kb → `python scripts/ingest_kb.py`（增量）→ 完成
3. 已知待办：mall 存量 212 条未重灌（无 channel 字段）——mall 检索目前不过滤（channel=None 时全库搜），需重灌打标后才可对 mall 启用过滤

## 6. 复杂文档入库（V18.1，2026-10-17）

解析层 `scripts/doc_parsers.py`（多格式统一产出 items，带 page/section/kind 元数据）：

| 格式 | 处理 |
|------|------|
| .pdf (PyMuPDF) | 跨页重复行识别**页眉/页脚**剔除（出现率 ≥40% 或页码模式）；span 字号众数=正文字号，**>1.15x 或加粗短行判为标题**（章节栈）；`find_tables` **表格转 markdown 行**（kind=table）；**脚注**（①②/注：开头）单独成块（kind=footnote）；**图片**可导出到 assets/ 并记引用块（kind=image，INGEST_EXTRACT_IMAGES 控制，默认开） |
| .md | #/##/### 标题栈 → 章节路径 |
| .txt/.json | 段落 / 原 FAQ 结构 |

语义切分 `semantic_chunks()`：
- 句子边界切分（。！？!?…；）——**绝不截断句中**
- 章节内贪心打包 ≤600 字；跨块重叠 = 上一块最后 1 句

payload 新增元数据：section（章节路径）、page、kind（text/table/image/footnote）。
验证：样例 PDF（含页眉/页码/章节/表格/脚注）→ 页眉页码剔除 ✅、脚注独立成块 ✅、4 points 入库 + knowledge_documents 镜像（channel 列）✅、幂等重跑跳过 ✅。
环境注意：MySQL host 端口 3307（容器内 3306）；bash 沙箱阻出站网络，跑入库需 dangerouslyDisableSandbox。
