# M14 业务闭环构造数据验证

> **目的**：用 mock 数据触发 M14 4 actions + RefundFlow 4 分支 + Tool 调用 + 边界 case，生成简历可用的数据背书。

## 目录结构

```
scripts/m14_validation/
├── README.md            # 本文件
├── mock_data.py         # 10 user + 30-50 order 构造 + DB 插入/清理
├── query_pool.py        # 100 business scenarios 生成
└── run_validation.py    # 主入口：临时启灰度开关 → 跑 100 scenarios → 生成报告
```

输出：

```
data/m14_validation/
├── raw.json                          # 原始结果（100 条）
├── failed_cases.json                 # 失败 case 详情
└── m14_validation_report.md          # 简历同步建议的 markdown 报告
```

## 4 核心指标

| 指标 | 公式 | 简历怎么写 |
|------|------|-----------|
| **主动查询覆盖率** | (DIRECT_ANSWER + SHOW_PICKER) 成功数 / Resolver 决策数 | "100 scenarios 验证 4 actions 决策分布，覆盖率 XX%" |
| **业务流程完成率** | RefundFlow.run() 走完所有 stage 数 / RefundFlow 调用数 | "RefundFlow 30 场景流程完成率 XX%" |
| **Tool 调用成功率** | Tool 返回成功数 / Tool 调用数 | "Tool 调用成功率 XX%（X/X）" |
| **Hallucination Free Rate** | 无异常 case 数 / 总 case 数 | "Hallucination Free Rate XX%（X/100）" |

## 用法

```bash
# 全流程：构造 + 验证 + 报告
PYTHONPATH=backend python scripts/m14_validation/run_validation.py

# 只清理（异常中断后）
PYTHONPATH=backend python scripts/m14_validation/run_validation.py --cleanup-only

# 保留 mock 数据（debug 用，不自动清理）
PYTHONPATH=backend python scripts/m14_validation/run_validation.py --keep-mock
```

## 设计原则（CLAUDE.md §3.4 + §9）

| 原则 | 体现 |
|------|------|
| 不修改业务代码 | ✅ 只读接口 + 调 `OrderContextResolver.resolve()` / `RefundFlow.run()` |
| 不修改数据库 schema | ✅ 用 user_id 10001-10010 隔离，hard delete by user_id |
| 不写 .env | ✅ 用 `settings.ENABLE_* = True` 临时改，跑完恢复 |
| 单模块 | ✅ scripts/ 目录独立，不污染 backend/app/ |
| 不调 chat API | ✅ 直接调 Resolver / RefundFlow，避开 SSE / 鉴权复杂度 |

## 100 scenarios 分布

| 类别 | 数量 | 验证什么 |
|------|------|---------|
| Resolver 4 actions | 40 | 主动查询覆盖率 |
| RefundFlow 4 分支 | 30 | 业务流程完成率 |
| Tool 调用 | 20 | Tool 调用成功率 |
| 边界 case | 10 | Hallucination Free Rate |

## mock 数据分布（10 user × 0-5 订单 = 26 订单）

| user_id | 订单数 | 验证 Resolver action |
|---------|--------|---------------------|
| 10001 | 0 | ASK_LOGIN_OR_LIST |
| 10002 | 1 | DIRECT_ANSWER only_one |
| 10003 | 2 | SHOW_PICKER disambiguate |
| 10004 | 3 | SHOW_PICKER |
| 10005 | 4 | SHOW_PICKER |
| 10006 | 5 | SHOW_PICKER（MAX_PICKER_ITEMS=5 边界）|
| 10007 | 5 | SHOW_PICKER（vip 多 SKU）|
| 10008 | 3 | user_provided_order_no 命中 |
| 10009 | 2 | NOT_FOUND（跨用户越权）|
| 10010 | 1 | 退款 4 分支覆盖（delivered 3 天）|

## 异常中断恢复

如果脚本异常中断但 mock 数据已插入，**手动清理**：

```sql
DELETE FROM orders WHERE user_id BETWEEN 10001 AND 10010;
```

或：

```bash
PYTHONPATH=backend python scripts/m14_validation/run_validation.py --cleanup-only
```

## 与简历的对应

报告自动生成"§5 简历同步建议"小节，包含 4 段可直接复制到简历的 bullet。完整路径：

`data/m14_validation/m14_validation_report.md` 第 5 节
