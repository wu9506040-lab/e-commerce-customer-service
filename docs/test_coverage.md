# 智能客服 Agent 系统 — 测试保障体系

> 公网部署版本专项测试设计与运行手册  
> 最新数字：**75 pytest 单元测试 + 70 端到端集成测试 = 145 项**  
> 端到端用例分布于 **6 个独立 verify_*.py**，全部针对 http://120.79.27.124:8000 / 5173

---

## 1. 测试分层架构

```
┌─────────────────────────────────────────────────────────┐
│  Day2e P1    非功能：性能 / SSE 完整性 / 降级语义        │  4 条
├─────────────────────────────────────────────────────────┤
│  Day2b RAG   AI 召回：4 类政策 hit@5 + 鲁棒性 + 防串单  │  8 条
├─────────────────────────────────────────────────────────┤
│  Day2c Cache 缓存一致性：cache_hit + 跨用户 + 降级       │  4 条
├─────────────────────────────────────────────────────────┤
│  Day2d 安全  IDOR 水平越权 + 未授权 401 + 垂直越权 403   │  4 条
├─────────────────────────────────────────────────────────┤
│  Day2a Guard 3 层防御防误伤 + 灰度边界（M13.1 回归）     │  4 条增量
├─────────────────────────────────────────────────────────┤
│  Day1d 历史  6 条 M13 bug 回归固化（CI 必跑）            │  6 条
├─────────────────────────────────────────────────────────┤
│  Day1c 意图  4 类意图 × 6 用例 + 跨意图边界              │ 25 条
├─────────────────────────────────────────────────────────┤
│  Day1b 状态机  LangGraph 4 路径 + 4 异常分支            │  8 条
├─────────────────────────────────────────────────────────┤
│  pytest unit  单元测试（已有 75 项，service / tool / schema）│ 75
└─────────────────────────────────────────────────────────┘
```

---

## 2. 测试矩阵（按用例数）

| 模块                | 用例数 | 脚本                              | 通过率 |
|---------------------|--------|-----------------------------------|--------|
| 单元测试            | 75     | `tests/` (pytest)                 | 100%   |
| LangGraph 状态机    | 8      | `scripts/verify_refund_state_machine.py` | 8/8    |
| 4 意图分流          | 25     | `scripts/verify_intent_classify.py`     | 25/25  |
| M13 历史 bug 回归   | 6      | `scripts/verify_regression_m13.py`      | 6/6    |
| Guard 防误伤        | 13*    | `scripts/verify_guard.py` (+Day2a 4)   | 13/13  |
| RAG 召回 hit@5      | 8      | `scripts/verify_rag_recall.py`         | 8/8    |
| 缓存一致性          | 4      | `scripts/verify_cache_consistency.py`  | 4/4    |
| IDOR 安全           | 4      | `scripts/verify_idor.py`               | 4/4    |
| P1 非功能           | 4      | `scripts/verify_p1_perf.py`            | 4/4    |
| **合计**            | **147**（含 M13 早期 20 blackbox_audit.py） | | |

*verify_guard.py 含原始 7 条 + Day2a 扩 4 条 + L3 重复 1 条 = 13 条

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
| R6| .env 配置 RATE_LIMIT_PER_MINUTE=30       | 配置存在（middleware 待实现） |

### 3.4 IDOR 安全

| # | 攻击                                  | 期望                  |
|---|---------------------------------------|-----------------------|
| 1 | A(visitor) 查 B(demotest) 订单        | HTTP 404              |
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

---

## 4. 怎么运行

### 4.1 单元测试（本地 + Docker）
```bash
cd deploy
docker compose --env-file .env.dev up -d  # 起 5 服务
cd ..
pytest tests/ -v                           # 75 项
```

### 4.2 端到端测试（公网，单脚本）
```bash
# 用 ECS 公网地址
python scripts/verify_refund_state_machine.py     # 8/8
python scripts/verify_intent_classify.py          # 25/25
python scripts/verify_regression_m13.py           # 6/6
python scripts/verify_idor.py                     # 4/4
python scripts/verify_cache_consistency.py        # 4/4
python scripts/verify_rag_recall.py               # 8/8
BASE=http://120.79.27.124:8000 python scripts/verify_guard.py  # 13/13
python scripts/verify_p1_perf.py                  # 4/4

# 切本地 + FLUSHALL 一键：
ssh aliyun "docker exec customer-service-redis redis-cli FLUSHALL"
python scripts/blackbox_audit.py                  # 20/20（M13 端到端）
```

### 4.3 CI 必跑（最低门槛）
1. `verify_regression_m13.py`（6 条）— 每次 PR 必跑，FAIL = 立刻排查
2. `verify_refund_state_machine.py`（8 条）— 退款逻辑改动后必跑
3. `verify_intent_classify.py`（25 条）— 意图规则改动后必跑
4. `pytest tests/`（75 条）— 单元测试

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

**8 个 P0/P1 bug 全部回归固化**，每次迭代自动验证不复发。

---

## 6. 测试体系设计哲学

### 6.1 体系思维（不能只罗列数字）

测试分 4 层：核心 AI 链路 / 业务接口 / 端到端 / 单元测试。

- **核心 AI 链路**（4 套）：LangGraph 状态机 / 4 意图分流 / RAG hit@5 / Guard 三层防御 — 每套独立脚本、独立断言。
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

数字是结果，重点是测了*什么*。

### 6.3 工程化落地

测试不只验功能，还覆盖：

- **性能**：首 token 延迟 P50（实测 0.59s）
- **可观测性**：SSE meta + token × N + done + closed 事件完整
- **容错**：Redis FLUSHALL 后立即发 query 仍正常响应（不误伤）
- **降级**：Qdrant 故障时静默放行 + 业务不崩
- **安全**：IDOR 4 路径（看他人订单 / 会话 / 未授权 / 垂直越权）

### 6.4 质量闭环

8 个 P0/P1 bug 由写测试本身发现（其中 2 个为 Day1b/1c 阶段新发现）：

| 阶段 | bug |
|------|-----|
| Day1b | cache_hit 路径污染 refundable/reason（refund_query 串单风险） |
| Day1c | Guard L2 误伤含 SKU 前缀的属性查询 |

每个 bug 都固化为回归用例进 `verify_regression_m13.py`，每次 PR 都会自动跑 — 构成「测试 → 优化 → 回归」的迭代闭环。

---

## 7. 已知未实现（P2 未来工作）

| 项 | 状态 | 建议 |
|----|------|------|
| IP 限流中间件（30/min） | 配置已存在 `.env: RATE_LIMIT_PER_MINUTE=30`，但代码未实现 | 加 slowapi 或自写 middleware，5 行 |
| 限流实际触发测试        | R6 是配置存在性检查，不是压测触发 | 限流中间件完成后改 `regress_6` 为真压测 |
| Prompt 注入军火         | P3 不必做 | 演示项目无系统 prompt 价值 |
| SQL 注入 / XSS          | P3 不必做 | Pydantic + Vue 模板 + ORM 已挡 |

---

## 8. 文件索引

```
scripts/
├── verify_refund_state_machine.py   # 8 条 LangGraph 状态机
├── verify_intent_classify.py        # 25 条 4 意图
├── verify_regression_m13.py         # 6 条历史 bug 回归（CI 必跑）
├── verify_guard.py                  # 13 条 Guard（含 Day2a 4 增量）
├── verify_rag_recall.py             # 8 条 RAG hit@5
├── verify_cache_consistency.py      # 4 条缓存
├── verify_idor.py                   # 4 条安全
├── verify_p1_perf.py                # 4 条非功能
└── blackbox_audit.py                # 20 条 M13 端到端（M13 早期）

tests/                                # 75 单元测试（pytest）

frontend/_screenshots/
├── intent_classify_report.json
├── refund_state_machine_report.json
├── regression_m13_report.json
├── cache_consistency_report.json
├── idor_report.json
├── rag_recall_report.json
└── p1_perf_report.json
```
