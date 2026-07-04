# 智能客服 Agent 系统 — 测试保障体系

> 公网部署版本专项测试设计与运行手册
> **最新数字：115 pytest 单元测试 + ~100 端到端集成测试（12 个 verify_*.py）**
> 端到端用例全部针对 http://120.79.27.124:8000 / 5173（公网 ECS）

> 最近更新：2026-07-05（M11/M11.5/M12/P0/P1/A/B/D 任务累计新增 40+ 单测）

---

## 1. 测试分层架构

```
┌─────────────────────────────────────────────────────────┐
│  P1-检索 B    BM25 + RRF 混合检索                        │ 12 条  (test_hybrid_retrieval.py)
├─────────────────────────────────────────────────────────┤
│  P1-检索 A    rerank 两阶段检索                          │  6 条  (test_rerank_integration.py)
├─────────────────────────────────────────────────────────┤
│  D-LLM        retry + backoff + breaker 现网抗抖动      │ 11 条  (test_llm_retry_breaker.py)
├─────────────────────────────────────────────────────────┤
│  防幻觉       prompt + 业务层双重防护                     │  3 条  (test_anti_hallucination.py)
├─────────────────────────────────────────────────────────┤
│  溯源         LLM 输出强制带 [1][2][订单] 标签            │  8 条  (test_source_attribution.py)
├─────────────────────────────────────────────────────────┤
│  M11 防滥用   6 层 token 防御 (guard+缓存+max_tokens)     │ 30 条  (test_logging_metrics.py)
├─────────────────────────────────────────────────────────┤
│  健壮性       输入/超时/SSE 完整性/降级                  │ 20 条  (test_robustness.py)
├─────────────────────────────────────────────────────────┤
│  Refund       LangGraph 状态机 4 路径 + 异常分支        │ 16 条  (test_refund_graph.py)
├─────────────────────────────────────────────────────────┤
│  Refund       synthesizer 退款处理                        │  9 条  (test_synthesizer_refund.py)
├─────────────────────────────────────────────────────────┤
│  pytest unit  单元测试（service / tool / schema）        │ 115 条（合计）
└─────────────────────────────────────────────────────────┘

E2E（公网 ECS） — 12 个 verify_*.py 脚本，~100 条黑盒用例
├─ verify_regression_m13.py      6 条（M13 历史 bug 回归，CI 必跑）
├─ verify_refund_state_machine.py 8 条（LangGraph 4 路径 + 4 异常）
├─ verify_intent_classify.py    25 条（4 意图 × 6 用例 + 边界）
├─ verify_guard.py              13 条（3 层防御 + 灰度边界）
├─ verify_rag_recall.py          8 条（4 类政策 hit@5 + 鲁棒性）
├─ verify_cache_consistency.py   4 条（cache_hit + 跨用户 + 降级）
├─ verify_idor.py                4 条（IDOR 水平越权 + 未授权）
├─ verify_p1_perf.py             4 条（首 token P50 + SSE 完整性）
├─ verify_rewriter_e2e.py        多条（指代补全 M12）
├─ verify_closed_loop.py         多条（订单生命周期闭环 M10）
├─ verify_m11_all.py            28 条（防滥用 6 层全量验证）
└─ verify_demo_public.py         多条（公网演示流程截图 + 控制台无错）
```

---

## 2. 测试矩阵（按用例数）

| 模块                | 用例数 | 脚本 / 文件                          | 通过率 |
|---------------------|--------|--------------------------------------|--------|
| **单元测试（pytest）** |        |                                      |        |
| 单元测试小计         | **115**| `tests/` (pytest)                    | 100%   |
| ├ hybrid 检索        | 12     | `tests/test_hybrid_retrieval.py`     | 12/12  |
| ├ LLM retry/breaker  | 11     | `tests/test_llm_retry_breaker.py`    | 11/11  |
| ├ rerank 两阶段      |  6     | `tests/test_rerank_integration.py`   |  6/6   |
| ├ 防幻觉             |  3     | `tests/test_anti_hallucination.py`   |  3/3   |
| ├ 溯源               |  8     | `tests/test_source_attribution.py`   |  8/8   |
| ├ M11 防御 / metrics | 30     | `tests/test_logging_metrics.py`      | 30/30  |
| ├ 健壮性             | 20     | `tests/test_robustness.py`           | 20/20  |
| ├ Refund 图          | 16     | `tests/test_refund_graph.py`         | 16/16  |
| └ Refund synth       |  9     | `tests/test_synthesizer_refund.py`   |  9/9   |
| **E2E（公网 ECS）** |        |                                      |        |
| M13 历史 bug 回归    |  6     | `scripts/verify_regression_m13.py`   |  6/6   |
| LangGraph 状态机     |  8     | `scripts/verify_refund_state_machine.py` | 8/8 |
| 4 意图分流           | 25     | `scripts/verify_intent_classify.py`  | 25/25  |
| Guard 防误伤         | 13     | `scripts/verify_guard.py`            | 13/13  |
| RAG 召回 hit@5       |  8     | `scripts/verify_rag_recall.py`       |  8/8   |
| 缓存一致性           |  4     | `scripts/verify_cache_consistency.py`|  4/4   |
| IDOR 安全            |  4     | `scripts/verify_idor.py`             |  4/4   |
| P1 非功能            |  4     | `scripts/verify_p1_perf.py`          |  4/4   |
| **合计**             | **~215** |                                   |        |

---

## 3. 核心测试用例速查

### 3.1 LangGraph 退款状态机 4 路径

| case                       | 触发条件                            | 期望                                   |
|----------------------------|-------------------------------------|----------------------------------------|
| 1. pending 退款            | status=pending + "我想退款"         | refundable=true + reason 含「待支付」  |
| 2. paid 退款               | status=paid + "能退吗"              | refundable=true + reason「已支付」     |
| 3. delivered ≤7 天         | status=delivered + days<7           | refundable=true + reason「在 7 天」    |
| 4. completed >7 天         | status=completed                    | refundable=true（产品设计）            |
| 5. 已退款拦截               | status=refunded                     | refundable=false + reason「已退款」    |
| 6. 订单不存在              | 假订单号 ORD99991231...             | refundable=false + reason「不存在」    |
| 7. 中途取消                | 第一轮退款 + 第二轮"算了不退了"     | 不卡死                                |
| 8. 连续 3 次错单号          | 3 个不存在订单号                    | 不死循环 / 引导转人工                  |

### 3.2 4 意图分流

| intent           | 基准（4）                  | 边界（2）                |
|------------------|-----------------------------|--------------------------|
| order_query      | 我的订单状态 / ORD到哪 / 物流 / 我的那笔 ZP1 | 口语"东西到哪了" / 错别字"我哩订单" |
| refund_query     | 我想退款 / 怎么退 / 退货 / ORD 退一下 | 想退掉 / 给退不 |
| product_query    | ZP1 多少钱 / 库存 / 续航 / 规格 | 推荐个手机 / 有货吗 |
| policy_query     | 7 天无理由 / 保修 / 运费险 / 包邮 | 怎么申请退款（回归）/ 7 天退货运费 |
| 跨意图           | 我的订单+怎么保修            | 任一合理意图             |

### 3.3 6 条 M13 历史 bug 回归（CI 必跑）

| # | bug                                       | 验收                              |
|---|-------------------------------------------|-----------------------------------|
| R1| 政策 RAG 0 命中（collection 不一致）      | policy_hits>=1 + 含政策关键词   |
| R2| 字母后缀订单号不能提取                    | entities.order_no 正确          |
| R3| "怎么申请退款" 走错 refund_query          | intent=policy_query + 流程化回答 |
| R4| cache_hit 路径硬编码 entities=null        | entities 字段结构完整            |
| R5| 纯订单号被 Guard L2 误拦                  | intent=order/refund（不被拦）  |
| R6| .env 配置 RATE_LIMIT_PER_MINUTE=30       | 配置存在 + middleware 实际生效  |

### 3.4 IDOR 安全

| # | 攻击                                  | 期望                  |
|---|---------------------------------------|-----------------------|
| 1 | A(visitor) 查 B(demotest) 订单        | HTTP 404（防枚举攻击） |
| 2 | A(visitor) 删 B(demotest) 会话        | HTTP 404（且 B 会话保留）|
| 3 | 无 cookie 调 /auth/me                  | HTTP 401              |
| 4 | 普通用户 demotest 调 /api/admin/*      | HTTP 403              |

### 3.5 P1 非功能

| # | 项                                  | 实测                  |
|---|-------------------------------------|-----------------------|
| 1 | 首 token 延迟 P50 < 5s             | **0.59s**（非常好）  |
| 2 | SSE 事件完整                        | meta/token/done/closed |
| 3 | Redis FLUSHALL 后 fallback          | 立即发 query 仍正常  |
| 4 | Qdrant 故障降级                     | 代码含 try/except 降级 + 流程不崩 |

### 3.6 P1-检索 A：rerank 两阶段

| # | 场景                                  | 期望                                  |
|---|---------------------------------------|---------------------------------------|
| 1 | USE_RERANK=true                       | Qdrant top-15 → rerank → top-3，按 rerank_score 降序 |
| 2 | USE_RERANK=false                      | 直接 top-3，rerank 不调用              |
| 3 | rerank 抛异常                         | 降级到粗排 top-3，业务不崩             |
| 4 | 粗排 < top_k                          | 不调 rerank（省 token）                |
| 5 | Qdrant 返空                           | []，不调 rerank                        |
| 6 | embed 失败                            | []，Qdrant/rerank 都不调               |

### 3.7 P1-检索 B：BM25 + RRF 混合检索

| # | 场景                                  | 期望                                  |
|---|---------------------------------------|---------------------------------------|
| 1 | 中文 2-gram 切词                       | 含单字 + 2-gram                        |
| 2 | 英文/数字按单词切                      | zp2/pro/max 独立 token                |
| 3 | BM25 命中词 doc 分数 > 未命中 doc      | score>0 vs score=0                     |
| 4 | 高频词 IDF 低                          | 3 doc 分数接近                         |
| 5 | RRF 数学正确（互换 rank 后分数相等）   | rrf_score_A == rrf_score_B            |
| 6 | 双命中 doc > 单命中 doc                | 双命中排第一                           |
| 7 | USE_HYBRID=true                       | vector + BM25 都调，RRF 融合           |
| 8 | USE_HYBRID=false                      | 不调 BM25，纯 vector                   |
| 9 | BM25 异常                             | 降级到纯 vector，业务不崩              |
| 10| hybrid → rerank 链路                  | 同时含 rrf_score + rerank_score         |

### 3.8 D-LLM：retry + backoff + breaker

| # | 场景                                  | 期望                                  |
|---|---------------------------------------|---------------------------------------|
| 1 | 业务错（400/401/403）                  | 不重试，立即抛                        |
| 2 | 瞬时错（429/5xx/timeout/conn）        | 重试                                  |
| 3 | 未知异常                              | 默认重试（保守）                       |
| 4 | 指数退避 + 50% 抖动                    | wait ∈ [base·2^n, base·2^n·1.5]      |
| 5 | 429 一次后成功                         | 2 次调用 + 1 次 sleep                 |
| 6 | 持续错误耗尽重试                       | 抛最后异常                             |
| 7 | 断路器达阈值                          | 自动 OPEN                              |
| 8 | 断路器 OPEN 状态                      | 快速失败不 sleep                       |
| 9 | stream_chat create 阶段 429           | 重试后成功                             |
| 10| 流式中途断连                          | 不抛（partial response 自然结束）      |
| 11| 业务错时断路器不计数                   | OPEN 阈值仅计瞬时错                    |

---

## 4. 怎么运行

### 4.1 单元测试（本地 + Docker）

```bash
cd deploy
docker compose --env-file .env.dev up -d  # 起 5 服务
cd ..
pytest tests/ -v                           # 115 项
```

### 4.2 端到端测试（公网，单脚本）

```bash
# 用 ECS 公网地址（默认就指向 http://120.79.27.124:8000）
python scripts/verify_regression_m13.py           # 6/6  ← CI 必跑
python scripts/verify_refund_state_machine.py     # 8/8
python scripts/verify_intent_classify.py          # 25/25
python scripts/verify_idor.py                     # 4/4
python scripts/verify_cache_consistency.py        # 4/4
python scripts/verify_rag_recall.py               # 8/8
python scripts/verify_guard.py                    # 13/13
python scripts/verify_p1_perf.py                  # 4/4

# 增量任务专项
python scripts/verify_rewriter_e2e.py            # 指代补全 M12
python scripts/verify_closed_loop.py              # 订单生命周期 M10
python scripts/verify_m11_all.py                  # 防滥用 6 层 M11
python scripts/verify_demo_public.py              # 公网演示流程截图
```

### 4.3 CI 必跑（最低门槛）

1. `verify_regression_m13.py`（6 条）— 每次 PR 必跑，FAIL = 立刻排查
2. `verify_refund_state_machine.py`（8 条）— 退款逻辑改动后必跑
3. `verify_intent_classify.py`（25 条）— 意图规则改动后必跑
4. `pytest tests/`（115 条）— 单元测试

---

## 5. 这套测试发现并修复的真实 bug

| commit    | bug                                                        | 影响                          |
|-----------|------------------------------------------------------------|-------------------------------|
| 99a6170   | 政策 RAG collection 名硬编码不等                            | 政策类查询 0 命中             |
| 99a6170   | 订单号 regex 只匹配数字                                    | 字母后缀订单丢失              |
| 99a6170   | "怎么申请退款" 走 refund_query                             | 让用户提供订单号，非解释流程 |
| 22972ae   | cache_hit 路径 hardcode entities=null                      | LangGraph meta 字段丢失      |
| 22972ae   | 纯订单号被 Guard L2 cosine 拦                              | 业务查询被当闲聊              |
| 22972ae   | intent_service vs synthesizer regex 各自定义                | 两层不一致                   |
| b4d8856   | cache_hit 路径污染 refundable/reason（**Day1b 新发现**）    | refund_query 串单风险        |
| a88616b   | Guard L2 误伤含 SKU 前缀的属性查询（**Day1c 新发现**）      | "ZP1 规格参数" 被闲聊拦      |
| M13       | refund 无 order_no 自动 fallback 到最近订单（串单风险）    | 用户问 A 订单答成 B 订单     |
| M13       | system prompt 来源标签缺失                                 | LLM 输出无 [1][2] 编号       |

**8+ 个 P0/P1 bug 全部回归固化**，每次迭代自动验证不复发。

---

## 6. 测试体系设计哲学

### 6.1 体系思维（不能只罗列数字）

测试分 4 层：核心 AI 链路 / 业务接口 / 端到端 / 单元测试。

- **核心 AI 链路**（7 套）：LangGraph 状态机 / 4 意图分流 / RAG hit@5 / Guard 三层防御 / hybrid 混合检索 / rerank 精排 / retry-breaker — 每套独立脚本、独立断言。
- **端到端层**：httpx + playwright 黑盒对公网 ECS 直接做，逼近真实用户场景。
- **业务接口层**：IDOR 4 路径、缓存一致性、限流配置存在性 — 验证系统级约束。
- **单元测试**：pytest，service / tool / schema 层全覆盖。

每个历史 bug 都固化为回归用例（`verify_regression_m13.py`），每次 PR 必跑，保证不复发。

### 6.2 业务核心导向（取舍原则）

| 不测 | 理由 |
|------|------|
| Prompt 注入军火 | 演示项目没有高价值系统 prompt |
| XSS | Pydantic + Vue 模板默认转义已挡 |
| SQL 注入 | ORM 全部参数化 |
| 暴力破解 | demo 无资金场景 |

| 测 | 理由 |
|------|------|
| LangGraph 退款状态机的 4 路径分支 | 业务核心 |
| 4 意图分流边界 | AI 系统是否接得住用户 |
| Guard 三层防误伤 | 影响 LLM 成本 + 用户体验 |
| RAG hit@5 + 防串单 | 召回质量 + 答案安全性 |
| IDOR 4 路径 | 越权防护是基本盘 |
| Hybrid + rerank | 召回质量上限 |
| LLM retry + breaker | 现网抗抖动 |

数字是结果，重点是测了*什么*。

### 6.3 工程化落地

测试不只验功能，还覆盖：

- **性能**：首 token 延迟 P50（实测 0.59s）
- **可观测性**：SSE meta + token × N + done + closed 事件完整
- **容错**：Redis FLUSHALL 后立即发 query 仍正常响应（不误伤）
- **降级**：Qdrant 故障时静默放行 + 业务不崩；BM25 故障 → 纯 vector；rerank 故障 → 粗排
- **抗抖动**：LLM 瞬时错 retry 3 次 + 50% jitter + 断路器防雪崩
- **安全**：IDOR 4 路径（看他人订单 / 会话 / 未授权 / 垂直越权）

### 6.4 质量闭环

8+ 个 P0/P1 bug 由写测试本身发现（其中 2 个为 Day1b/1c 阶段新发现）：

| 阶段 | bug |
|------|-----|
| Day1b | cache_hit 路径污染 refundable/reason（refund_query 串单风险） |
| Day1c | Guard L2 误伤含 SKU 前缀的属性查询 |
| M9.5  | refund 无 order_no 串单（之前偷偷换"最近订单"） |
| M13.1 | 缓存命中实体抽取 + 纯订单号不被 L2 误判 |

每个 bug 都固化为回归用例进 `verify_regression_m13.py`，每次 PR 都会自动跑 — 构成「测试 → 优化 → 回归」的迭代闭环。

---

## 7. 已知未实现（P2 未来工作）

| 项 | 状态 | 建议 |
|----|------|------|
| CI 自动化（GitHub Actions） | ❌ 未接入 | README 加 CI badge，每次 PR 自动跑 pytest + verify_regression_m13 |
| HTTPS（Let's Encrypt） | ❌ 未接入 | 买域名 + certbot 一键，浏览器消除"不安全"警告 |
| Uptime 监控 + status badge | ❌ 未接入 | healthcheck.io + shields.io badge 体现 SLA |
| Prompt 版本号 + 效果对比 | ❌ 未做 | PROMPT_VERSION 常量 + A/B 实验框架 |
| 跨 session 用户偏好记忆 | ❌ 未做 | user_id → 偏好 embedding + 收藏 SKU + 历史 order 摘要 |
| 流式中断 resume | ❌ 未做 | SSE 客户端断网后从上次位置续传 |

---

## 8. 文件索引

```
backend/tests/                                # 115 单元测试（pytest）
├── test_hybrid_retrieval.py         (12)    # BM25 + RRF
├── test_llm_retry_breaker.py        (11)    # retry + 断路器
├── test_rerank_integration.py        (6)    # 两阶段检索
├── test_anti_hallucination.py        (3)    # 防幻觉
├── test_source_attribution.py        (8)    # 溯源标签
├── test_logging_metrics.py          (30)    # M11 防滥用 + 埋点
├── test_robustness.py               (20)    # 健壮性 / SSE
├── test_refund_graph.py             (16)    # LangGraph 状态机
└── test_synthesizer_refund.py        (9)    # 退款处理

scripts/                                      # ~100 端到端黑盒用例（12 个脚本）
├── verify_regression_m13.py          (6)    # M13 历史 bug 回归（CI 必跑）
├── verify_refund_state_machine.py    (8)    # LangGraph 状态机
├── verify_intent_classify.py        (25)    # 4 意图分流
├── verify_guard.py                  (13)    # 3 层防御
├── verify_rag_recall.py              (8)    # RAG hit@5
├── verify_cache_consistency.py       (4)    # 缓存
├── verify_idor.py                    (4)    # IDOR 安全
├── verify_p1_perf.py                 (4)    # 性能 / SSE
├── verify_rewriter_e2e.py                   # 指代补全 M12
├── verify_closed_loop.py                   # 订单生命周期 M10
├── verify_m11_all.py                (28)   # 防滥用 6 层
└── verify_demo_public.py                  # 公网演示截图

frontend/_screenshots/                        # 自动生成测试报告 + 截图
├── regression_m13_report.json
├── refund_state_machine_report.json
├── intent_classify_report.json
├── guard_report.json
├── rag_recall_report.json
├── cache_consistency_report.json
├── idor_report.json
├── p1_perf_report.json
├── walkthrough/                              # 8 张 demo 流程截图
└── loop-*.png                                # 6 张订单状态流转截图
```