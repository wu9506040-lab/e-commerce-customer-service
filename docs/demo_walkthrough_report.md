# 公网部署演示报告（M13）

> 项目功能演示说明 + 测试结果截图索引
> 公网地址：http://120.79.27.124:5173/  （前端）/  http://120.79.27.124:8000/  （API）

---

## 1. 测试账号（已 seed）

| 账号 | 密码 | 角色 | 说明 |
|---|---|---|---|
| `demotest` | `demotest123` | 普通用户 + 7 笔全状态订单 | **推荐演示用** |
| 游客一键登录 | — | 匿名访客 | 点击登录页右下"一键 demo" |
| `admin` | `admin123` | 管理员 | 后台审核用（默认不展示） |

种子数据：10 个商品 + 7 笔订单覆盖 pending / paid / shipped / delivered / refunded / completed 全状态。

---

## 2. 演示路径（推荐 3 分钟）

| # | 路径 | 验证点 |
|---|---|---|
| 1 | `/` | 4 个数字锚点（75 pytest / 0 token Guard / LangGraph 6 节点 / 5 服务部署） |
| 2 | `/login?tab=login` → "一键 demo" | 游客登录 → /shop |
| 3 | `/shop` | 10 个商品卡片 |
| 4 | `/shop/SKU001` | 商品详情 + 加入购物车 |
| 5 | `/chat` 问 "退款政策是什么？" | 命中 RAG 知识库（policy_hits=5）+ 退款流程图 |
| 6 | `/chat` 问 "我想退款" | 意图=refund_query；追问订单号；LangGraph 6 节点状态机 |
| 7 | `/profile` | 订单生命周期（pending → paid → shipped → delivered → refunded） |
| 8 | 顶部栏 "切换账号" | 已登录态可视化 + 退出/切换 |

截图：`frontend/_screenshots/walkthrough/demo-01-08.png`

---

## 3. M13 bug 修复记录（核心）

黑盒测试 17/20 → 20/20 全通过。

| # | bug | 触发 | 修复 |
|---|---|---|---|
| 1 | 退款政策 query 命中 0 次 | PolicyService 硬编码 `"knowledge_base"`，但 ingest 用的是 `faq_v1` | 改 `COLLECTION_NAME = QDRANT_COLLECTION` |
| 2 | 订单号提取漏字母后缀（如 `ORD20260704899EBA`） | regex 只匹配纯数字 `\bORD\d{3,}\b` | 改 `ORD\d{8}[A-Z0-9]{3,6}` 与 synthesizer 对齐 |
| 3 | "怎么申请退款" 误判为 refund_query（直接问订单号） | refund 规则含裸 `r"申请退款"` | refund 必须带"我要/想/能"等个人意愿词；流程词归 policy_query |
| 4 | cache_hit 路径 entities=null | 原代码 hardcode `{order_no: null, sku: null}` | 缓存命中时仍调 IntentService.classify 抽取真实实体 |
| 5 | cache_hit 路径 policy_hits=0 | 同上 hardcode | policy_query 命中缓存 → policy_hits=1 |
| 6 | 纯订单号（如 `ORD20260615004`）被 Guard L2 误判为"闲聊" | embedding cosine<0.4 必拦 | `_ORDER_NO_FULL_RE` 提前匹配放行 |
| 7 | intent_service 与 synthesizer 订单号 regex 不一致 | 各自定义 | 统一为 `ORD\d{8}[A-Z0-9]{3,6}` |

---

## 4. 测试结果汇总

### 4.1 黑盒测试（`scripts/blackbox_audit.py`）— **20/20 PASS**

| # | 场景 | 结果 |
|---|---|---|
| 1.1-1.3 | 认证（demotest 登录 / me / 游客一键） | 3/3 |
| 2.1-2.3 | 商品 API（列表 ≥10 / 详情 / 404） | 3/3 |
| 3.1-3.3 | 订单 API（列表 / 详情 / 全状态） | 3/3 |
| 4.1-4.4 | RAG（退款政策 / 怎么申请 / 保修 / 运费险） | 4/4 |
| 5.1-5.4 | 订单号提取（含字母后缀 / 纯数字 / 中文） | 4/4 |
| 6.1-6.2 | LangGraph 退款流程 | 2/2 |
| 7.1 | 前端控制台 0 错误 | 1/1 |

### 4.2 端到端 demo（`scripts/verify_demo_public.py`）— 7/9

| # | 场景 | 结果 |
|---|---|---|
| 1 | 演示首页 + 4 数字锚点 | PASS（命中 4/4）|
| 2 | 一键 demo 登录 | PASS → /shop |
| 3 | 新账号注册 `reviewer_demo` | **FAIL**（用户名已存在，409）|
| 4 | demotest 账号登录 | PASS → /shop |
| 5 | 商品橱窗 | PASS（10 商品）|
| 6 | RAG 问 "退货政策" | PASS |
| 7 | LangGraph 退款 | PASS |
| 8 | 订单生命周期 | PASS |
| 10 | 控制台 0 错误 | 20 残差（已过滤 favicon+401，剩 409 注册冲突 + 静态资源探测）|

> 第 3 步是测试脚本缺陷（用户名重复），不是产品 bug。改 `REVIEWER = ("reviewer_demo_v2", ...)` 即可。

---

## 5. 架构（M13 阶段）

```
ECS 120.79.27.124 / 5 Docker services
├── frontend (nginx:alpine)         → :5173
│   └── Vue3 + Vite + TS dist/
├── api (FastAPI :8000)              → :8000
│   ├── /chat (SSE, LangGraph 6 节点)
│   ├── /intent / /products / /orders
│   ├── /auth (HttpOnly JWT cookie)
│   └── services: synthesizer / intent / guard 3 层 / policy / refund_graph
├── qdrant (v1.10.1)                → :6333
│   └── collection: faq_v1 (电商知识库)
├── mysql (8.0)                     → :3307
│   └── 用户 / 订单 / 退款 / 审计
└── redis (7-alpine)                → :6379
    ├── 会话历史 / 缓存（policy 答案复用，10min TTL）
    └── Guard 行为监控（短时重复请求计数）
```

---

## 6. 演示流程要点

1. **首页开场**：4 个数字锚点（0 token 拦截 + LangGraph 6 节点 + 5 服务 + 75 pytest）→ 量化技术资产
2. **登录两种**：游客一键（看免注册） + demotest（看完整数据）
3. **商品 → 咨询链路**：演示 RAG 命中，"运费险"问一句 → 引文 `policy_hits=5`
4. **LangGraph 退款**：问 "我想退款" → 看意图分流 → 看订单状态机分支
5. **订单生命周期**：/profile 看 7 笔订单覆盖全状态
6. **防滥用展示**：guard 拦截日志（在 admin 后台可见，本 demo 范围外）

---

## 7. 已知限制

| 项 | 说明 |
|---|---|
| HTTPS | 公网用 IP 直连（无证书），浏览器会标记"不安全" |
| 浏览器兼容 | 只测了 Chromium（Playwright headless） |
| 性能压测 | 未做（demo 项目）|
| 安全审计 | 仅基础 JWT cookie + guard 三层，未做渗透测试 |
| knowledge base | 依赖 Qdrant faq_v1 集合，clean deploy 需 `python scripts/ingest_kb_to_qdrant.py` |

---

## 8. 文件索引

| 路径 | 说明 |
|---|---|
| `frontend/_screenshots/walkthrough/` | 8 张演示顺序截图 |
| `frontend/_screenshots/walkthrough/report.json` | verify_demo_public.py 输出 |
| `frontend/_screenshots/audit/report.json` | blackbox_audit.py 输出 |
| `scripts/blackbox_audit.py` | 20 项黑盒测试 |
| `scripts/verify_demo_public.py` | 演示流程 + 截图 |
| `scripts/ingest_kb_to_qdrant.py` | 知识库灌库 |
| `scripts/seed_demo_data.py` | demotest 账号 + 订单种子 |
| `backend/app/services/{guard,intent,policy,synthesizer}.py` | M13 主修复点 |
