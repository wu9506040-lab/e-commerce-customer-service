# AI Engineering SOP（任务分级 + 正逆双向审查 + 数据准确性验证）

> 文档代号：SOP-V1
> 文档版本：V1.0（2026-07-16）
> 文档状态：✅ 与 CLAUDE.md 配套执行
> 适用范围：方案设计阶段、涉及数据库 / 接口 / 业务逻辑的开发任务
> 不适用：纯文字 typo、不影响行为的文档排版
> 维护者：Tech Lead
> 关联文档：CLAUDE.md §4 AI 6 步法 / §6 验证标准

---

## 1. AI 任务自动分级与 SOP 触发

### 1.1 分级规则（每次接任务先判断）

| Level | 触发条件示例 | SOP 要求 |
|---|---|---|
| **L0** | 文档修改 / 注释修改 / 样式微调 / 小范围重构（< 50 行、不跨模块、不动业务） | 无需正逆 SOP |
| **L1** | 新增函数 / 新增接口 / 修改业务逻辑 / 修改已有流程 | **简版正逆分析**（4 类强制） |
| **L2** | 数据库变更 / API 契约变化 / Agent·Tool·RAG 流程变化 / 跨模块修改 / 状态机变化 / 权限安全相关 / 部署配置变化 | **完整正逆 SOP**（8 类强制） |

**判断不出 → 默认提升一级**（宁严勿松）。

### 1.2 决策辅助表

| 任务 | Level | 理由 |
|---|---|---|
| 改 README 错字 | L0 | 文档 |
| 加 1 个纯函数（无 IO） | L1 | 新增函数 |
| 加 1 个 API endpoint（含入参校验） | L1 | 新增接口 |
| 改 SQLAlchemy 模型加字段 | L2 | 数据库变更 |
| 改 `/api/chat` 入参 schema | L2 | API 契约变化 |
| 修业务方法命名 bug | L1 | 修改业务逻辑 |
| 加新 Agent ToolSpec | L2 | Agent 流程变化 |
| 加新业务规则到 Prompt | L2 | LLM 流程 |
| 改订单状态机 | L2 | 状态机变化 |
| 加权限校验 | L2 | 权限安全 |
| 改 docker-compose.yml | L2 | 部署配置 |
| 跨 chat + rag + emotion 3 模块 | L2 | 跨模块 |
| 单文件 < 50 行重构 | L0 | 小范围重构 |
| 判断不出 | +1 级 | 默认从严 |

### 1.3 Level 1：简版正逆分析（4 类强制）

| # | 类别 | 必填说明 |
|---|---|---|
| 1 | 参数异常 | 校验失败返什么码 / 字段 |
| 2 | 业务边界 | 空数据 / 越界 / 重复 / 状态非法 |
| 3 | 现有调用方兼容 | 签名变化是否影响其他模块 |
| 4 | 单 commit 回滚 | 改动可独立回退 |

### 1.4 Level 2：完整正逆 SOP（8 类强制）

L1 的 4 类 + 以下 4 类，缺一不可：

| # | 类别 | 必填说明 |
|---|---|---|
| 5 | 权限失败 | 是否区分 401 / 403 |
| 6 | 第三方服务失败 | 降级策略 / cache / 重试退避 |
| 7 | LLM 异常 | fallback 到 V1.2 / token 限流 |
| 8 | 数据回滚 | 事务 / 配置 / 灰度关闭 |

### 1.5 正向流程（L1+ 必填）

| 项 | 内容 |
|---|---|
| 正常业务路径 | happy path 描述 |
| 数据流 | 输入 → 处理 → 输出 |
| 调用链 | 模块 / 服务 / 客户端 |
| 接口 / Schema 变化 | 草稿 + 调用方影响 |
| 配置变化 | 新增 / 修改 / 灰度开关 |
| 测试方案 | 单元 + 集成 + e2e |
| 回滚隔离策略 | commit 边界 / 灰度开关 / 数据库回滚 |

### 1.6 模板（追加在 CLAUDE.md §4.1 任务模板"方案"小节后）

```markdown
### 任务分级
- [ ] Level: L0 / L1 / L2（判断不出默认 +1 级）
- 判断依据：[列出 1-2 条]

### 逆向流程审查（按 Level 强制覆盖）
| 异常类型 | Level | 处理策略 | 是否已有机制 |
|---|---|---|---|
| 参数异常 | L1+ | | |
| 业务边界 | L1+ | | |
| 调用方兼容 | L1+ | | |
| 单 commit 回滚 | L1+ | | |
| 权限失败 | L2 only | | |
| 第三方服务失败 | L2 only | | |
| LLM 异常 | L2 only | | |
| 数据回滚 | L2 only | | |
```

---

## 2. 数据准确性验证规范

> 触发条件：本 § 与 §1 任务分级正交；L2 数据库变更 + 任何 L1+ 接口/业务逻辑改动 = 必须走本 § 四要素。

### 2.1 四要素（缺一不可）

| # | 要素 | 必交付内容 |
|---|---|---|
| 1 | DB Schema | 表结构、字段类型、约束、迁移脚本 |
| 2 | 测试数据 | `tests/fixtures/` 含 schema 说明 + seed 数据 + mock 数据 |
| 3 | 接口断言 | curl 命中 + 状态码 + payload 字段 + 异常路径 |
| 4 | 业务结果断言 | DB 状态 / 状态流转 / 关联关系 / 副作用 |

### 2.2 测试数据规范（`tests/fixtures/`）

推荐目录结构：

```text
tests/fixtures/
├── schema/             # 表结构说明（可选 markdown）
├── seed/               # 种子数据（脱敏后入库）
│   ├── orders.json
│   └── users.json
└── mock/               # mock 数据（LLM / 第三方 API）
    └── llm_responses.json
```

约束：

| # | 约束 | 原因 |
|---|---|---|
| 1 | 种子数据必须脱敏（无真实手机号 / 邮箱 / 姓名） | 隐私 + 合规 |
| 2 | LLM mock 数据必须覆盖正常 / 异常 / 边界三类 | 单测覆盖 |
| 3 | seed 数据规模 ≤ 100 条 / 文件 | pytest 启动快 |

### 2.3 DB 断言规范（pytest + SQLAlchemy）

```python
def test_order_status_flow(db_session):
    # Arrange: 用 fixture 创建订单
    order = create_test_order(db_session, user_id="u_test", status="pending")

    # Act: 触发状态流转
    response = client.post(f"/api/orders/{order.id}/pay")

    # Assert 1: HTTP 状态码
    assert response.status_code == 200

    # Assert 2: 业务结果（DB 断言，禁止只测 HTTP 200）
    db_session.refresh(order)
    assert order.status == "paid"

    # Assert 3: 关联关系
    assert order.payment is not None
    assert order.payment.amount == order.amount

    # Assert 4: 副作用
    assert get_logistics(order.id).status == "preparing"
```

验证清单（每条 DB 相关测试必须覆盖）：

| # | 验证项 | 示例 |
|---|---|---|
| 1 | 数据是否正确写入 | `assert row.value == expected` |
| 2 | 字段是否符合预期 | `assert order.status in valid_states` |
| 3 | 状态流转是否正确 | `pending → paid → shipped → delivered` |
| 4 | 关联关系是否正确 | 外键 / 中间表 / 关联对象 |

### 2.4 禁止行为

| # | 禁止 | 原因 |
|---|---|---|
| 1 | 只测 HTTP 200 | 状态码不能证明业务逻辑正确 |
| 2 | 只 mock 不验证 DB | mock 不能证明真实持久化正确 |
| 3 | 测试不隔离 | 污染 dev / 其他测试 |
| 4 | seed 含 PII | 用户隐私 + 合规风险 |

### 2.5 现有项目注意事项

| # | 注意项 | 状态 |
|---|---|---|
| 1 | 独立 test DB | 本项目**目前没有**，DB 断言落地前需先建基础设施（test profile 或事务回滚） |
| 2 | `tests/fixtures/` 目录 | **目前不存在**，建议作为本 SOP 的 v1.1 增量（不在本次范围） |
| 3 | `tests/conftest.py` | **已存在**，可作为 fixture 入口 |

---

> 与 CLAUDE.md 关系：本 SOP 是 CLAUDE.md §4 Step 2 和 §6 的**执行细则**，不替代、不重写现有规则。
> 冲突时优先级：CLAUDE.md > 本文件 > docs/governance/ai_development_rules.md。