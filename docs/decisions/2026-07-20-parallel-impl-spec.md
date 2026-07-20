# Sprint 14/15/17 并行实施 Spec（2026-07-20 · 主 agent 定接口）

> **目的**：主 agent 主导定 3 个 Protocol 接口 + Schema + 测试契约 + 文件边界，
> 然后启动 3 个 subagent 在 worktree 隔离并行实施。
>
> **状态**：🔄 spec 待用户审核 → 启动 subagent → 主 agent 集成
>
> **背景**：用户拍板「业务场景完整 + 架构解耦 + 数据可不全」；
> 当前 Sprint 14/15/17 是**完全独立的 3 个架构抽象**（无相互 import），
> 是多 agent 并行的最佳切入点。理论节省 1-2 周（8 周 → 6-7 周）。

---

## 0. 并行 Sprint 总览

| Sprint | 主题 | 估时 | 依赖 | subagent |
|--------|------|------|------|----------|
| **S14** | ChannelAdapter Protocol + Webhook 默认实现 | 5d | 无 | agent-A |
| **S15** | OrderService + ProductService Protocol 抽象 + MySQL 默认实现 | 5-7d | 无 | agent-B |
| **S17** | KnowledgeSource Protocol 抽象 + Qdrant 默认实现 | 3-5d | 无 | agent-C |

**并行性保证**：3 个 Sprint 之间 0 相互 import（验证：`grep -rn "from app.channels" backend/app/services/` 应为 0；其他方向同理）。

---

## 1. Sprint 14 Spec · ChannelAdapter Protocol

### 1.1 业务定位

让任何支持 Webhook / API 回调的系统（自建商城 / 内部 OA / SaaS / IM）能接入 AI 客服。
ChannelAdapter 是**消息收发层**，不关心业务数据（订单/商品由 OrderService/ProductService 负责）。

### 1.2 Protocol 定义（位于 `backend/app/channels/protocols.py`）

```python
"""
ChannelAdapter Protocol（CLAUDE.md §9.9 落地）

任意 IM / 商城 / SaaS 系统的接入抽象。
默认实现：Webhook（最通用）。其他实现：微信公众号 / 钉钉 / 飞书（V3+ YAGNI）。
"""
from typing import Protocol, Optional, List
from app.schemas.channel_event import ChannelEvent, ChannelReply


class ChannelAdapter(Protocol):
    """通道适配器协议 — 接入方实现该接口即可让 AI 客服对接自家系统"""

    channel_type: str  # "webhook" | "wechat" | "dingtalk" | ...

    async def receive(self, payload: dict, headers: dict) -> ChannelEvent:
        """
        接收外部系统的消息（webhook 调用 / 长连接回调）
        入参：原始 payload + headers（接入方自定义鉴权）
        出参：标准化 ChannelEvent（user_id / message / metadata）
        异常：InvalidSignatureError / UnsupportedMessageTypeError
        """
        ...

    async def send(self, event: ChannelEvent, reply: ChannelReply) -> dict:
        """
        向外部系统发送 AI 回复
        入参：原始事件（用于回传 context）+ AI 回复
        出参：发送结果（接入方 API 返回值）
        异常：SendMessageError / RateLimitError
        """
        ...

    async def get_user_info(self, user_id: str) -> dict:
        """
        获取接入方用户信息（昵称 / 头像 / 等级 / 历史订单）
        接入方决定字段；AI 客服仅取 needed 字段
        """
        ...


class ChannelAdapterFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""

    def get(self, channel_type: str) -> ChannelAdapter: ...


# === 异常类（位于 protocols.py 同文件） ===
class ChannelError(Exception): ...
class InvalidSignatureError(ChannelError): ...
class UnsupportedMessageTypeError(ChannelError): ...
class SendMessageError(ChannelError): ...
class RateLimitError(ChannelError): ...
```

### 1.3 Pydantic Schema（位于 `backend/app/schemas/channel_event.py`）

```python
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class ChannelEvent(BaseModel):
    """标准化通道事件（ChannelAdapter.receive 出参）"""
    channel_type: str = Field(..., description="通道类型（webhook / wechat / ...）")
    channel_user_id: str = Field(..., description="接入方用户 ID")
    channel_session_id: str = Field(..., description="接入方会话 ID（用于回传 context）")
    message: str = Field(..., min_length=1, max_length=2000)
    message_type: str = Field("text", description="text | image | file | event")
    metadata: dict = Field(default_factory=dict, description="接入方自定义字段")
    timestamp: datetime = Field(..., description="事件时间")


class ChannelReply(BaseModel):
    """标准化 AI 回复（ChannelAdapter.send 入参）"""
    text: str = Field(..., description="回复文本")
    cards: Optional[List[dict]] = Field(None, description="结构化卡片（OrderCard 等）")
    metadata: dict = Field(default_factory=dict, description="回调 metadata")
```

### 1.4 测试契约（subagent 必过 · 6 用例）

| # | 测试 | 断言 |
|---|------|------|
| 1 | `test_webhook_receive_text_message` | 标准 webhook payload → ChannelEvent 字段全对 |
| 2 | `test_webhook_receive_invalid_signature` | 错误签名 → InvalidSignatureError |
| 3 | `test_webhook_send_text_reply` | mock httpx → 正确 POST 到 callback URL |
| 4 | `test_webhook_send_rate_limit` | mock 429 → RateLimitError |
| 5 | `test_webhook_get_user_info` | mock GET → 返接入方用户 dict |
| 6 | `test_factory_returns_webhook_adapter` | `get("webhook")` → WebhookAdapter 实例 |

### 1.5 文件边界

| 操作 | 路径 | 行数预算 |
|------|------|---------|
| 新建 | `backend/app/channels/__init__.py` | 5 |
| 新建 | `backend/app/channels/protocols.py` | < 100 |
| 新建 | `backend/app/channels/webhook_impl.py` | < 250 |
| 新建 | `backend/app/schemas/channel_event.py` | < 80 |
| 新建 | `backend/app/api/channels.py`（webhook 接收端点） | < 100 |
| 新建 | `backend/tests/test_channel_webhook.py` | 6 用例 |
| 修改 | `backend/app/main.py`（注册 channels router） | +5 行 |

### 1.6 8 件套交付（CLAUDE.md §9.8）

1. 模块职责：`docs/learning_log.md` 追加章节
2. 接口契约：`app/channels/protocols.py`
3. 输入输出：`app/schemas/channel_event.py`
4. ORM / 数据模型：无（接入方决定数据格式）
5. 依赖关系：上游 = 接入方系统；下游 = ChatService（未来接入）
6. 调用流程：Mermaid sequence diagram（webhook → receive → ChatService → send）
7. 测试方案：单测 6 用例（mock httpx）
8. 已知限制：仅 webhook 实现；IM Adapter 留 V3+

---

## 2. Sprint 15 Spec · OrderService + ProductService Protocol

### 2.1 业务定位

把当前 RefundTool / OrderTool / ProductTool 直接 `from app.clients.mysql_client import ...` 的硬编码，
抽成 Protocol + MySQL 默认实现。接入方自己实现 Protocol 即可用自己的订单/商品系统。

### 2.2 Protocol 定义（位于 `backend/app/services/order/protocols.py`）

```python
"""
OrderService + ProductService Protocol（CLAUDE.md §9.3.2 落地）

接入方实现该接口即可对接自家订单/商品系统。
默认实现：MySQLImpl（基于现有 OrderTool/ProductTool 重构）。
"""
from typing import Protocol, List, Optional
from datetime import datetime
from app.schemas.business import Order, OrderItem, Product, ProductQuery


class OrderService(Protocol):
    """订单服务协议"""

    async def get_order(self, user_id: int, order_no: str) -> Optional[Order]:
        """按订单号查询订单（含 items）；不存在返 None"""
        ...

    async def list_user_orders(
        self, user_id: int, status: Optional[str] = None,
        start_date: Optional[datetime] = None, end_date: Optional[datetime] = None,
        limit: int = 20, cursor: Optional[str] = None,
    ) -> tuple[List[Order], Optional[str]]:
        """查询用户订单列表；返 (orders, next_cursor)"""
        ...

    async def get_order_status(self, order_no: str) -> Optional[str]:
        """查订单当前状态（pending / paid / shipped / completed / cancelled）"""
        ...


class ProductService(Protocol):
    """商品服务协议"""

    async def get_product(self, sku: str) -> Optional[Product]:
        """按 SKU 查商品详情；不存在返 None"""
        ...

    async def search_products(
        self, query: str, category: Optional[str] = None,
        limit: int = 10,
    ) -> List[Product]:
        """关键词搜索商品（query + 可选分类）"""
        ...

    async def get_recommendations(
        self, user_id: int, context_skus: List[str], limit: int = 5,
    ) -> List[Product]:
        """基于上下文 SKU 推荐相似商品（主动营销用）"""
        ...


class OrderServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_order_service(self) -> OrderService: ...
    def get_product_service(self) -> ProductService: ...


# === 异常类 ===
class OrderError(Exception): ...
class OrderNotFoundError(OrderError): ...
class ProductError(Exception): ...
class ProductNotFoundError(ProductError): ...
```

### 2.3 Pydantic Schema（位于 `backend/app/schemas/business.py`）

```python
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    sku: str
    product_name: str
    quantity: int
    unit_price: float
    subtotal: float


class Order(BaseModel):
    order_no: str
    user_id: int
    status: str  # pending / paid / shipped / completed / cancelled / refunding / refunded
    items: List[OrderItem]
    total_amount: float
    shipping_address: Optional[str] = None
    tracking_no: Optional[str] = None
    create_time: datetime
    update_time: datetime


class Product(BaseModel):
    sku: str
    name: str
    category: Optional[str] = None
    price: float
    stock: int
    description: Optional[str] = None
    images: List[str] = Field(default_factory=list)


class ProductQuery(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    category: Optional[str] = None
    limit: int = Field(10, ge=1, le=50)
```

### 2.4 测试契约（subagent 必过 · 10 用例）

| # | 测试 | 断言 |
|---|------|------|
| 1 | `test_order_mysql_impl_get_order` | mock session → Order 字段全对 |
| 2 | `test_order_mysql_impl_get_order_not_found` | 订单不存在 → 返 None |
| 3 | `test_order_mysql_impl_list_user_orders` | 多订单 + cursor 分页 |
| 4 | `test_order_mysql_impl_filter_by_status` | status 过滤生效 |
| 5 | `test_product_mysql_impl_get_product` | mock session → Product 全字段 |
| 6 | `test_product_mysql_impl_search_products` | LIKE 搜索 + limit |
| 7 | `test_product_mysql_impl_get_recommendations` | 基于 context_skus 推荐 |
| 8 | `test_factory_returns_mysql_impl` | `get_order_service()` → MySQLOrderService |
| 9 | `test_refund_tool_uses_protocol` | RefundTool 改用 Protocol（mock 替换） |
| 10 | `test_order_tool_uses_protocol` | OrderTool 改用 Protocol（mock 替换） |

### 2.5 文件边界

| 操作 | 路径 | 行数预算 |
|------|------|---------|
| 新建 | `backend/app/services/order/__init__.py` | 5 |
| 新建 | `backend/app/services/order/protocols.py` | < 100 |
| 新建 | `backend/app/services/order/mysql_impl.py` | < 300 |
| 新建 | `backend/app/services/order/factory.py` | < 50 |
| 新建 | `backend/app/schemas/business.py` | < 100 |
| 修改 | `backend/app/tools/refund_tool.py`（用 Protocol） | 重构 ≤ 80 行 |
| 修改 | `backend/app/tools/order_tool.py`（用 Protocol） | 重构 ≤ 80 行 |
| 修改 | `backend/app/tools/product_tool.py`（用 Protocol） | 重构 ≤ 80 行 |
| 新建 | `backend/tests/test_order_protocol.py` | 10 用例 |

### 2.6 8 件套交付

1. 模块职责：`docs/learning_log.md` 追加章节
2. 接口契约：`app/services/order/protocols.py`
3. 输入输出：`app/schemas/business.py`
4. ORM / 数据模型：复用现有 Order/Product ORM（不新增表）
5. 依赖关系：上游 = 接入方系统；下游 = RefundTool/OrderTool/ProductTool
6. 调用流程：Mermaid（Tool → Protocol → MySQLImpl）
7. 测试方案：单测 10 用例（mock Protocol）
8. 已知限制：仅 MySQL 默认实现；接入方自定义实现留 V3+

---

## 3. Sprint 17 Spec · KnowledgeSource Protocol

### 3.1 业务定位

把当前 PolicyService 直接 `from app.clients.qdrant_client import ...` 的硬编码，
抽成 Protocol + Qdrant 默认实现。接入方可用 Elasticsearch / 文件系统 / 任何知识库接入。

### 3.2 Protocol 定义（位于 `backend/app/rag/protocols.py`）

```python
"""
KnowledgeSource Protocol（CLAUDE.md §9.3.3 落地）

任意知识库接入抽象。当前默认实现：QdrantImpl + 混合检索（BM25 + 向量）。
"""
from typing import Protocol, List, Optional
from app.schemas.knowledge import SearchResult, Document


class KnowledgeSource(Protocol):
    """知识库协议"""

    source_type: str  # "qdrant" | "elasticsearch" | "filesystem" | ...

    async def search(
        self, query: str, top_k: int = 5,
        filters: Optional[dict] = None,
    ) -> List[SearchResult]:
        """关键词/语义检索 → 召回 top_k"""
        ...

    async def get_document(self, doc_id: str) -> Optional[Document]:
        """按 doc_id 取完整文档（用于反幻觉审计）"""
        ...

    async def upsert(self, document: Document) -> str:
        """新增/更新文档 → 返 doc_id"""
        ...

    async def delete(self, doc_id: str) -> bool:
        """删除文档 → 返是否成功"""
        ...


class KnowledgeSourceFactory(Protocol):
    def get(self, source_type: str = "qdrant") -> KnowledgeSource: ...


# === 异常类 ===
class KnowledgeError(Exception): ...
class DocumentNotFoundError(KnowledgeError): ...
```

### 3.3 Pydantic Schema（位于 `backend/app/schemas/knowledge.py`）

```python
from typing import Optional
from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    doc_id: str
    content: str
    score: float = Field(..., ge=0.0, le=1.0, description="相似度分数")
    metadata: dict = Field(default_factory=dict)


class Document(BaseModel):
    doc_id: Optional[str] = None
    title: str
    content: str
    category: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
```

### 3.4 测试契约（subagent 必过 · 6 用例）

| # | 测试 | 断言 |
|---|------|------|
| 1 | `test_qdrant_search_returns_results` | mock qdrant → SearchResult 字段全对 |
| 2 | `test_qdrant_search_with_filters` | filters 生效（category / metadata） |
| 3 | `test_qdrant_get_document_not_found` | doc_id 不存在 → 返 None |
| 4 | `test_qdrant_upsert_returns_doc_id` | mock upsert → 返新 doc_id |
| 5 | `test_policy_service_uses_protocol` | PolicyService 改用 Protocol（mock 替换） |
| 6 | `test_factory_returns_qdrant_impl` | `get("qdrant")` → QdrantKnowledgeSource |

### 3.5 文件边界

| 操作 | 路径 | 行数预算 |
|------|------|---------|
| 新建 | `backend/app/rag/__init__.py`（顶层 rag/ 目录，按 CLAUDE.md §7.1）| 5 |
| 新建 | `backend/app/rag/protocols.py` | < 60 |
| 新建 | `backend/app/rag/qdrant_impl.py` | < 200 |
| 新建 | `backend/app/rag/factory.py` | < 50 |
| 新建 | `backend/app/schemas/knowledge.py` | < 50 |
| 修改 | `backend/app/services/policy_service.py`（用 Protocol） | 重构 ≤ 200 行 |
| 新建 | `backend/tests/test_knowledge_protocol.py` | 6 用例 |
| 修改 | `backend/app/core/config.py` | + `KNOWLEDGE_SOURCE_TYPE` 配置 |

> ⚠️ **范围说明**：CLAUDE.md §7.1 规划顶层 `rag/` 目录但用户决议「不迁 `services/rag/`」；
> Sprint 17 新建**顶层** `app/rag/`（与 §7.1 一致），不动 `services/rag/`。

### 3.6 8 件套交付

1. 模块职责：`docs/learning_log.md` 追加章节
2. 接口契约：`app/rag/protocols.py`
3. 输入输出：`app/schemas/knowledge.py`
4. ORM / 数据模型：无（Qdrant 持久化）
5. 依赖关系：上游 = PolicyService；下游 = Qdrant Client
6. 调用流程：Mermaid（PolicyService.search → Protocol → Qdrant）
7. 测试方案：单测 6 用例（mock Qdrant Client）
8. 已知限制：仅 Qdrant 默认实现；BM25 复用现有 rag/pipeline.py

---

## 4. subagent 协作协议（主 agent ↔ subagent）

### 4.1 分发协议

主 agent 给每个 subagent 的 prompt 必须包含：
1. **本 spec 文档路径**（让 subagent 自学）
2. **目标 Sprint 编号**（S14 / S15 / S17）
3. **Protocol 接口签名**（从 spec §1.2 / §2.2 / §3.2 复制）
4. **测试契约清单**（subagent 必过的 N 用例）
5. **文件边界**（新建/修改清单 + 行数预算）
6. **worktree 路径**（subagent 在 worktree 工作）
7. **交付要求**：8 件套（CLAUDE.md §9.8）

### 4.2 subagent 必读

- CLAUDE.md §5（Scope Lock · 单模块）
- CLAUDE.md §9（架构设计要求）
- CLAUDE.md §9.3.3（AI 能力必须抽象）
- CLAUDE.md §9.8（8 件套交付）
- CLAUDE.md §4.4（Stop-Loss 8 问 · commit 独立可回滚）
- CLAUDE.md §4.5（AI Review 五项检查单）

### 4.3 subagent 禁止

- ❌ 修改本 spec 文档
- ❌ 跨文件改其他 Sprint 的产物
- ❌ 直接 `from app.clients.mysql_client import`（必须走 Protocol）
- ❌ 引入新依赖（除非 spec 明确允许）
- ❌ 超过文件行数预算

### 4.4 交付物格式

每个 subagent 完成后输出：
```
## Sprint 14 完成报告
- commit hash: [xxx]
- 新建文件：[列表 + 行数]
- 修改文件：[列表 + 改动行数]
- 测试：N 用例 PASS
- 8 件套：learning_log §XX 已追加
- 已知限制：[列表]
```

---

## 5. 集成方案（主 agent 收尾）

### 5.1 merge 顺序

```
1. merge agent-B (S15 Order/Product)     # 最早：核心数据层
2. merge agent-A (S14 Channel)            # 中：消息层
3. merge agent-C (S17 Knowledge)          # 最后：知识层
```

理由：S15 是 S18 业务场景的前置依赖，必须先稳定。

### 5.2 冲突处理

预期冲突点：
- `backend/app/main.py`（每个 agent 都注册自己的 router → 用 `add_blocks` 分块合并）
- `backend/app/core/config.py`（配置项冲突 → 顺序合并）

主 agent 用 git 三方合并（Ours/Theirs 自动判断 + 手工检查）。

### 5.3 全量回归

```
pytest tests/                                # 全量（目标 +30 用例）
grep -rn "from app.clients.mysql_client import" backend/app/tools/  # 0 命中
grep -rn "from app.clients.qdrant_client import" backend/app/services/policy_service.py  # 0 命中
python -m py_compile backend/app/channels/*.py  # 语法检查
```

### 5.4 验收清单

- [ ] 3 个 Sprint 全部 commit + 测试 PASS
- [ ] 全量 pytest 无回归（基线 473 + 新增 ≥ 22 用例 = 495+）
- [ ] Protocol 契约测试覆盖（mock 实现可替换）
- [ ] 8 件套完整（learning_log §52 / §53 / §54）
- [ ] 双 remote 已 push（Gitee + GitHub）

---

## 6. 时间线

```
Day 0  : 用户审 spec（本文件）            [当前]
Day 0-1: 主 agent 修订 spec（如有）       [按用户反馈]
Day 1  : 启动 3 个 subagent 并行          [关键节点]
Day 6-8: subagent 完成各自 Sprint          [并行截止]
Day 7-9: 主 agent 集成 + 全量回归         [收尾]
Day 9  : learning_log §52/§53/§54         [知识沉淀]
```

---

## 7. 不在本次范围

| 项 | 原因 | 推后到 |
|----|------|--------|
| S16 RefundService / LogisticsService | 依赖 S15 接口定稿 | S15 完成后启动 |
| S18 业务场景补完 | 强依赖 3 个 Protocol 稳定 | Sprint 14/15/17 完成后 |
| S20 数据导入 | 依赖 S15-17 Protocol 稳定 | Sprint 15/17 完成后 |
| 第二个实现（接入方自定义） | YAGNI §3.3 | 等真实接入方出现 |
| 多租户（Sprint 6 升级） | 不阻塞当前并行 | S22 单独规划 |

---

## 8. 待用户拍板事项

| # | 决策点 | 推荐 | 备选 |
|---|--------|------|------|
| 1 | Sprint 14 是否包含 IM Adapter（微信公众号测试号） | ❌ 不包含（仅 Webhook） | ✅ 包含 |
| 2 | Sprint 15 是否包含 RefundService Protocol | ❌ 不包含（S16 单独） | ✅ 包含 |
| 3 | Sprint 17 是否引入 BM25 索引重构 | ❌ 不引入（复用现有） | ✅ 引入 |
| 4 | Protocol 同步 vs 异步方法 | ✅ 全部 async（FastAPI 友好） | sync |
| 5 | 集成时主 agent 是否亲自跑全量回归 | ✅ 亲自跑 | subagent 各自负责 |

**默认按"推荐"列执行**；如用户对某项有异议，请指出。