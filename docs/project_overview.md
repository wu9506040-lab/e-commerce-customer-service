# 项目全景介绍 — 智能客服中台（智能选电商客服系统）

> 定位：**企业级 AI 客服中台**——不是闲鱼自动回复脚本，而是"多渠道接入 + AI 应答管线 + 人工坐席工作台 + 数据中台"的完整客服系统。
> 代码：`E:\智能客服`（GitHub/Gitee 私有仓库，311+ commits）

---

## 一、项目整体架构

```
┌─────────────────────────── 渠道层 ───────────────────────────┐
│  闲鱼 IM（自研 WebSocket 协议栈）      网页商城（Vue3 Web）      │
└──────────────┬────────────────────────────┬─────────────────┘
               ▼                            ▼
┌─────────────────────── 接入/应答层 ──────────────────────────┐
│ AccountWorker × N（多账号，每账号独立）                         │
│  XianyuWsClient（收发/心跳/补拉） → DebounceQueue（合并）       │
│   → ReplyScheduler（意图路由→场景分诊→分 Agent prompt→LLM）     │
│   → ReplyValidator（数据校验）→ MessageGuard（安全）            │
│   → 打字模拟 → 分段发送                                        │
│ AdminBridge（Redis 命令/事件总线，人工后台 ⇄ daemon）           │
└──────────────┬────────────────────────────┬─────────────────┘
               ▼                            ▼
┌─────────────────────── AI/数据层 ────────────────────────────┐
│ FastAPI /chat（SSE 流式，快速通道+RAG 管线）                    │
│ Qdrant RAG（多渠道隔离 + 增量入库）  Redis（会话热数据/事件流）   │
└──────────────┬────────────────────────────┬─────────────────┘
               ▼                            ▼
┌─────────────────────── 人工/运营层 ──────────────────────────┐
│ 人工坐席工作台（三栏）  账号管理  数据看板  工单  外发通知         │
│              （全部 Vue3 + ECharts，Ant Design Pro 风格）       │
└──────────────────────────────────────────────────────────────┘
```

**一个买家消息的完整旅程**（闲鱼渠道）：
1. 闲鱼服务器推送 → 自研 ws 客户端解码（LWP 帧 + MessagePack）
2. ackDiff 补拉完整内容 + message_id 去重 + 噪声过滤
3. 4s debounce 合并连发 → ConversationRecorder **即时落库**
4. 本地意图路由（price/tech/default，零 LLM 开销）→ 场景分诊（售前/售中/售后）
5. 快速通道：system_prompt 直出 LLM（跳过 RAG）→ SSE 流式收字
6. ReplyValidator 校验（格式/占位符/长度/价格幻觉）→ MessageGuard 安全过滤
7. 打字模拟延迟 → 长回复分段 → ws 发送（无限重试）
8. 全程事件上行 Redis → 人工工作台实时可见；转人工自动建单 + 外发通知

---

## 二、目录结构

```
E:\智能客服\
├── backend/                     # Python 后端（FastAPI）
│   ├── app/
│   │   ├── api/                 # HTTP 路由层
│   │   │   ├── chat.py          #   /api/chat SSE 流式对话（核心）
│   │   │   ├── auth.py          #   登录/JWT/管理员鉴权
│   │   │   ├── shop.py / public.py / conversations.py   # 商城域
│   │   │   ├── xianyu_admin.py  #   人工后台 API（会话/直发/接管/工单/通知）
│   │   │   ├── admin_handoff.py #   转人工工单（认领/回复/结单）
│   │   │   ├── admin_analytics.py # 数据看板/KPI 聚合
│   │   │   └── admin_conversations.py # 全局会话查询
│   │   ├── services/
│   │   │   ├── chat/            # 编排器（意图分派/快速通道/fallback）
│   │   │   ├── xianyu/          # ★ 闲鱼渠道全部逻辑（30 个模块，见下）
│   │   │   ├── policy_service.py#   RAG 检索（多路+重排+渠道过滤）
│   │   │   ├── kpi_service.py / analytics_service.py # 统计
│   │   │   └── handoff_ticket_service.py # 工单状态机
│   │   ├── services/xianyu/     # 闲鱼渠道模块明细：
│   │   │   ├── xianyu_ws_client.py      # 自研 WebSocket 协议栈（1800+ 行）
│   │   │   ├── account_manager.py / account_worker.py  # 多账号管理
│   │   │   ├── reply_scheduler.py       # 回复调度中枢
│   │   │   ├── reply_validator.py       # 回复数据校验管道
│   │   │   ├── message_guard.py         # 敏感词/站外引流过滤
│   │   │   ├── conversation_recorder.py # 消息实时落库 + 订单证据扫描
│   │   │   ├── admin_bridge.py          # Redis 命令/事件总线
│   │   │   ├── notification_service.py  # 外发通知（钉钉/企微/飞书/微信）
│   │   │   ├── cookie_renew_service.py / cookie_browser_refresh.py # Cookie 四层自愈
│   │   │   ├── escalation_detector.py / manual_mode.py # 转人工/接管
│   │   │   └── debounce_queue / bargain_counter / health / ...
│   │   ├── rag/                 # RAG 抽象层（KnowledgeSource 协议 + Qdrant 实现）
│   │   ├── models/              # SQLAlchemy ORM（message/conversation/order/
│   │   │                        #   handoff_ticket/xianyu_order/user/...）
│   │   ├── core/                # 配置/日志/熔断器
│   │   └── clients/             # MySQL/Redis/Qdrant 客户端
│   ├── config/                  # accounts.json（账号注册表）/ notify.json（通知）
│   ├── scripts/                 # 运维脚本（ingest_kb.py / run_xianyu_daemon.py…）
│   └── tests/                   # 97 个测试文件（2.3 万行）
├── frontend/                    # Vue3 + Vite + TS
│   └── src/
│       ├── views/               # 商城页（Shop/Chat/Product）+ 管理台
│       │   ├── XianyuWorkbench.vue  # ★ 人工坐席工作台（三栏）
│       │   ├── AccountsAdmin.vue    # 多账号管理
│       │   ├── DataBoard.vue        # 数据看板（ECharts）
│       │   ├── AdminLogin.vue / AdminLayout.vue  # 管理台专属登录/布局
│       │   └── HandoffWorkbench.vue / AdminAnalytics.vue / AdminKpi.vue
│       ├── components/          # AdminLayout（深色侧边栏）/AppNav/…
│       └── api.ts               # 统一 fetch 封装（cookie 鉴权）
├── deploy/                      # Docker Compose（5 容器）+ nginx + mysql init
├── docs/                        # 架构文档/审计报告/吸收笔记（10+ 篇）
│   ├── rag_data_platform.md     #   RAG 数据中台设计
│   ├── project_report.md        #   汇报版项目报告
│   └── reference_absorption_notes.md # 参考项目逐模块对照笔记
├── scripts/                     # daemon 启动/watchdog/入库流水线/评测脚本
├── docs/ecommerce_kb/           # 知识库源文件（JSON/PDF/MD → Qdrant）
├── cookies/ + cookie.txt        # 各账号 cookie（敏感，不入库）
├── logs/                        # daemon 日志/状态
└── tests/ backend/tests/        # 测试
```

---

## 三、核心功能清单（按模块）

### A. 闲鱼渠道接入
| 功能 | 说明 |
|------|------|
| 自研 WebSocket 协议栈 | LWP 帧/注册/心跳/ACK，MessagePack 解码，mtop.h5 签名——零第三方依赖 |
| ackDiff 补拉 | 轻量通知主动拉全量 + 60s 周期兜底，消息不丢 |
| 消息去重/合批 | message_id 组合键去重；4s debounce 合并连发（标点 1.5s 快发） |
| 多账号并行 | AccountWorker × N 独立 ws/调度/状态；账号注册表热重载；自家账号防 AI 回环 |
| Cookie 四层自愈 | L1 mtop Set-Cookie 重签 → L2 HTTP 续期 → L3 Playwright 免密登录 → L4 人工 |

### B. AI 应答管线
| 功能 | 说明 |
|------|------|
| 三级意图路由 | 本地关键词（零 LLM）→ 规则 → LLM 分类；price/tech/default 分 Agent prompt |
| 场景分诊 | 售前/售中/售后 YAML 话术库（few-shot），阶段自动检测 |
| 议价系统 | 议价轮次计数 + 赠品梯度策略（不二价框架） |
| 退款订单校验 | 扫聊天记录交易卡片判定订单状态：无单直接告知/有单建 P1 工单转专员 |
| 平台退款识别 | "我发起了退款申请"卡片 → P0 工单 + 受理话术 |
| 回复数据校验 | markdown/占位符/角标剥离、长度保险丝、价格幻觉检测（白名单对比） |
| 安全过滤 | 承诺性敏感词拦截、站外引流替换、空回复兜底 |
| 拟人化 | 打字模拟（字数比例延迟）、长回复句界分段、40 字口语约束 |

### C. 人工协同
| 功能 | 说明 |
|------|------|
| 三栏坐席工作台 | 会话列表（未读/状态标签）/对话窗（AI 浅蓝·人工深蓝·买家灰）/客户信息 |
| 一键接管/恢复 | Redis 命令总线 → daemon ManualMode，接管期 AI 静默 |
| 人工直发 | 经 daemon ws 通道 + 无限重试，落库标记 human |
| 自动建单 | 转人工/退款/投诉 → 工单（P0/P1/P2）带订单证据+可读摘要 |
| 快捷短语 | 对话窗底部芯片点击填入 |

### D. 数据中台
| 功能 | 说明 |
|------|------|
| 多格式入库 | PDF（页眉剔除/章节/表格/脚注）/MD/TXT/JSON → 语义分块 → 向量化 |
| 渠道隔离 | Qdrant payload.channel + 检索过滤，闲鱼/商城 KB 互不干扰 |
| 增量幂等 | content_hash 去重，重跑零成本 |
| 消息实时落库 | 所有渠道消息即时入 MySQL（含 account 归属） |

### E. 运营管理
| 功能 | 说明 |
|------|------|
| 人工后台管理台 | 专属登录页 + 深色布局（Ant Design Pro 风格） |
| 数据看板 | ECharts 11 图：消息趋势/渠道/峰值/响应时间/热门问题 TOP10/AI 率/客户标签 |
| 多账号管理页 | 连接状态灯/启停/重启/注册表重载 |
| 数据看板（真实） | 运营驾驶舱 + KPI 大盘（真实统计数据） |
| 外发通知 | 钉钉/企微/飞书/WxPusher/通用 webhook，工单事件自动推送 |

---

## 四、技术栈详情

| 技术 | 用途 |
|------|------|
| **Python 3.11 + FastAPI** | 后端 API + SSE 流式输出 |
| **websockets + MessagePack** | 闲鱼 IM 私有协议栈（自研实现） |
| **Vue3 + Vite + TypeScript** | 商城前端 + 管理台（双 SPA 路由域） |
| **ECharts** | 数据看板 11 图 |
| **MySQL 8** | 消息/订单/工单/用户/知识文档镜像（多渠道视图隔离） |
| **Redis** | 会话热历史、写穿冷路径、命令/事件总线、多账号状态上报 |
| **Qdrant** | 向量库（text-embedding-v3 1024 维，payload 渠道过滤） |
| **通义千问 qwen-plus** | 主对话 LLM（SSE 流式）；text-embedding-v3；qwen rerank |
| **BM25 + RRF** | 混合检索融合（与向量召回互补） |
| **Playwright** | Cookie L3 免密登录（系统 Chrome 反指纹） |
| **SQLAlchemy + Pydantic** | ORM 与校验 |
| **Docker Compose** | api/frontend/mysql/redis/qdrant 五容器编排 |
| **bcrypt/JWT** | 管理台认证（httpOnly cookie） |
| **bcrypt Watchdog** | daemon 进程探活自动拉起 |

---

## 五、部署方式

```bash
# 1. 基础设施（五容器，unless-stopped 自愈）
cd deploy && docker compose up -d

# 2. 前端构建（bind-mount 到 nginx）
cd frontend && npm install && npm run build

# 3. 闲鱼 daemon（宿主机进程，cookie 为宿主机文件）
python scripts/run_xianyu_daemon.py

# 4. 看门狗（30s 探活自动拉起 daemon）
python scripts/watchdog_xianyu_daemon.py
#   ↑ 生产应注册 Windows 任务计划/systemd 开机自启

# 5. 知识库入库（增量，幂等）
python backend/scripts/ingest_kb.py

# 访问
商城：      http://localhost:5173
人工后台：  http://localhost:5173/admin/xianyu   （admin 登录）
数据看板：  http://localhost:5173/admin/board
API：       http://localhost:8000/docs
```

环境配置：`deploy/.env.dev`（MySQL/Redis/Qdrant/QWEN_API_KEY）；账号：`backend/config/accounts.json`；通知：`backend/config/notify.json`。

---

## 六、目前还缺什么（TODO）

| 项 | 说明 | 优先级 |
|----|------|--------|
| 自动发货 | 订单状态监控（付款卡片触发）+ 发货规则配置 + 卡券自动发货/实物物流通知；需闲鱼发货 mtop 接口 | 高（路线图第 4 项） |
| 数据看板接真实数据 | 演示数据 → messages/latency_ms 真实聚合（SQL 都有基础） | 高 |
| 外发通知 UI 化配置 | 当前编辑 notify.json；做管理页表单 | 中 |
| 通知渠道扩展 | 按参考项目补 Telegram/PushPlus（骨架已留） | 低 |
| 扫码添加新账号 | 阶段 3：复用 xianyu-qr 容器做免 F12 加号 | 中 |
| 死信队列 | 多次发送失败的消息持久化补发 | 低 |
| RAG 真实数据切换 | 看板演示数据 → 真实统计 | 随看板 |
| 多账号压测 | >5 账号时线程/连接数验证 | 低 |
```
