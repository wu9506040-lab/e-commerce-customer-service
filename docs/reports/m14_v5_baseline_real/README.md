# M14 V5 baseline（metric 缺陷修复后真实话术验证 · 2026-07-19 22:08）

> **验证时间**: 2026-07-19 22:08 · ECS 公网 · 100 case · 耗时 82.2s
> **对比基线**: `docs/reports/m14_v4_baseline_real/`（V4 报告 4 失败 · metric 缺陷）
> **本次变更**: 修 `scripts/m14_validation/answer_quality.py`（V5 P2 任务：ref 无关键词返 None 而非硬编码 1.0）+ `run_validation.py`（跳过 None case）+ 上传 ECS 重跑
> **不改动**: 业务代码、配置、prompt、数据库结构

---

## 1. 关键根因（先看 · V5 metric 缺陷闭环 + 结构发现）

V4 报告标注的"P2 metric 缺陷"在 V5 闭环。修复后发现两层真因：

| 层级 | 真因 | 证据 |
|------|------|------|
| **L1 metric bug** | `answer_quality.py:91-92` ref 无 POLICY_KEYWORDS 时硬编码 `coverage_rate=1.0` | V4 16 case 中 4 个空 ref 贡献 25% 分子（实为无效数据）|
| **L2 结构发现** | 修复后真实数据 0/12 — agent 在 `ask_order_no` 分支不写政策文本 | 12 个有效 case 全部在 ask_order_no 分支，agent 输出 "您有 N 个订单..."（UI 占位），不包含 ref 中的 "24小时/7天无理由" 等政策术语 |

**关键简化**：不是 KB 没用，是**测错了** — 12 个 ask_order_no case 本就不该测政策覆盖率。

---

## 2. V4 baseline → V5 baseline 对比（5 真指标）

| 指标 | V4 baseline | V5 baseline | Δ | 结论 |
|------|---------|---------|---|------|
| **Resolver 准确率** | **96%** (48/50) | **96%** (48/50) | 持平 | ✅ 业务决策稳定 |
| **RefundFlow 分支** | **96.7%** (29/30) | **96.7%** (29/30) | 持平 | ✅ decide 节点 3 层决策稳定 |
| **Tool 调用** | **100%** (20/20) | **100%** (20/20) | 持平 | ✅ OrderTool 稳定 |
| **真幻觉率** ⬇️ | **1%** (1/100) | **0%** (0/100) | -1pp | ✅ LLM 非确定性，本次幸运（M14-0070 伪单号未触发）|
| **政策覆盖率** ⬆️ | **25%** (4.0/16 · 含 4 个无效) | **0%** (0.0/12 · 全部有效) | -25pp | ⚠️ metric 修复后真实数据；结构性问题见 §3 |

**核心结论**：
- ✅ L1 metric 缺陷闭环（4 个无效 case 不再稀释）
- ✅ LLM 4 大主指标（Resolver/RefundFlow/Tool/Hallucination）维持 V4 baseline
- ⚠️ 政策覆盖率需要重新定义（详见 §3）

---

## 3. ⚠️ policy_coverage 0% 的结构真因（不是 KB 问题）

### 3.1 V5 metric 修复成功（先确认）

| 维度 | V4（缺陷）| V5（修复后）|
|------|---------|---------|
| 16 个 case 总 ref 关键词数 | 18 | 18 |
| 有效 case 数（ref 含关键词）| 12 | **12** ✅ |
| None case 数（ref 无关键词）| 4 | **4** ✅ 正确排除 |
| coverage_sum | 4.0（4 个 None 硬编码 1.0 + 12 个真值 0）| **0.0**（12 个真值 0）✅ |
| policy_coverage | 25% | **0%** ✅ 真实 |

**L1 修复确认**：denominator 从 16 → 12，4 个 None case 正确不计入。

### 3.2 L2 结构问题：12 个有效 case 全部在 ask_order_no 分支

V5 raw.json 数据：

```
M14-0041: rate=0.0000  ref_kw=['24小时']         agent: 您有 4 个订单，请选择要退款的订单：
M14-0043: rate=0.0000  ref_kw=['签收', '24小时'] agent: 您有 3 个订单，请选择要退款的订单：
M14-0044: rate=0.0000  ref_kw=['签收', '24小时'] agent: 您有 4 个订单，请选择要退款的订单：
M14-0046: rate=0.0000  ref_kw=['质量问题']       agent: 您有 5 个订单，请选择要退款的订单：
M14-0047: rate=0.0000  ref_kw=['7天无理由']      agent: 您有 3 个订单，请选择要退款的订单：
...
```

**所有 12 个有效 case 的 agent 输出都是 "您有 N 个订单，请选择..."** — 这是 RefundFlow 的 ask_order_no 分支的 UI 占位符（订单选择列表），不是政策解释。

**根本原因**：reference_answer 描述的是"完整退款流程"（含 "24小时处理"、"7天无理由"），但 agent 当前只走到"订单选择"这一步。后续 synthesize 分支才会生成政策文本。

### 3.3 metric 定义建议（V6 P2 任务）

```python
# 当前（V5）：所有有 ref 的 case 都评估 coverage
# 建议（V6）：只评估 expected_branch in (synthesize,) 的 case
if scenario.expected in ("synthesize",):
    coverage = evaluate_coverage(...)
else:
    coverage = None  # ask_order_no / escalate / invalid_order 分支无政策输出
```

**预期效果**：
- 0 个 synthesize case 当前（V4-V5 都没触发）→ 真实数据无法测 KB 政策贡献
- 建议：在 real_corpus.json 加几个 expected=synthesize 的 case + 修复 RefundFlow 让 synthesize 真正触发

---

## 4. 5 真指标分布（V5）

**Resolvers 4 Actions（V5 同 V4）**：

| Action | 触发 | V4 同 |
|--------|-----|------|
| DIRECT_ANSWER | 18 (36%) | ✅ |
| SHOW_PICKER | 26 (52%) | ✅ |
| ASK_LOGIN | 3 (6%) | ✅ |
| NOT_FOUND | 3 (6%) | ✅ |

**RefundFlow 4 分支（V5 同 V4）**：

| 分支 | 触发 | V4 同 |
|------|-----|------|
| ask_order_no | 15 (50%) | ✅ |
| escalate | 11 (36.7%) | ✅ |
| invalid_order | 3 (10%) | ✅ |
| synthesize | **0 (0%)** | ✅（关键：synthesize 分支未触发，无法测 KB 政策贡献）|

---

## 5. 3 条失败 Case 详情（V5）

| # | ID | Corpus | Expected | Actual | 根因 | 优先级 |
|---|----|--------|----------|--------|------|--------|
| 1 | M14-0062 | RC054 | escalate | unknown | mock 数据 cleanup 阶段 DB 偶发；`detect_p0_escalate` 未识别（query="我花了 3000 块买的..."，属"compensation"非 4 类 P0 关键词） | P3（与 V3 同根因）|
| 2 | M14-0096 | RC017 | not_found | direct_answer | "订单 ORD20260718001 退货运费谁出？" 命中 policy_query 短路 → non_order_intent；订单归属校验被跳过 | P2（与 V3 同）|
| 3 | M14-0099 | — | direct_answer | not_found | "最近那个订单 ORD20260718001 快递..." 长 query 抽取到 order_no + 归属校验 → not_found；user 10004 不应拥有此单 | P2（与 V3 同）|

**分类小结**：
- 1 条 DB 偶发 + P0 关键词未覆盖（P3）
- 2 条边缘 case（归属校验反向触发，P2）

V4 的 M14-0070 伪单号幻觉在 V5 未触发（LLM 非确定性）。

---

## 6. ECS 上 V5 部署状态（2026-07-19 22:08）

| 组件 | 状态 | 验证 |
|------|------|------|
| `run_validation.py`（V5 修复）| ✅ 上传 | docker cp 进入容器（绕过 COPY scripts/ 缓存） |
| `answer_quality.py`（V5 修复）| ✅ 上传 | 同上 |
| `time.sleep(0.3)` 批间 throttle | ✅ | 82.2s（V4 78.2s + V5 修复后略增）|
| 5 灰度开关 | ORDER_RESOLVER + BUSINESS_FLOW + SSE_CARD_V2 临时启 | ✅ |

### 6.1 Mock 数据全流程

| 阶段 | 数量 |
|------|------|
| 运行前 mock 订单数 | 0 |
| 脚本自动插入 | 57 |
| `finally cleanup` 清理 | 57 |
| 运行后 mock 订单数 | 0 |

---

## 7. 关键代码 Commit 链路

| Commit | 关联 | 影响 |
|--------|------|------|
| 3188043 | run_validation.py P0 修复（V3 baseline 闭环）| V4 复用此修复 |
| **bd43504** | **V4 fix E1-E3** | 修复 V4 regression |
| dfa905f | M3 部署治本（volume mount + entrypoint）| KB 自动恢复 |
| **V5 本批 commit** | **evaluate_coverage None 处理 + run_validation.py 跳过 None** | metric 缺陷闭环 |

---

## 8. 残留待办（独立任务 · 不在本批）

| 优先级 | 任务 | 工时 |
|--------|------|------|
| **P0** | **real_corpus.json 加 expected=synthesize case + 触发 RefundFlow synthesize 分支**（**关键**：否则无法测 KB 政策贡献）| 60 min |
| P2 | V6 metric 只评估 expected_branch=synthesize 的 case | 15 min |
| P2 | M14-0068 防伪规则业务层加固 | 30 min |
| P2 | Resolver policy_query + order_no entity 归属校验 | 30 min |
| P2 | refund_graph unknown 分支兜底 | 30 min |
| P2 | CI env diff 检查（`.env` vs `.env.dev`）| 30 min |

---

## 9. 数据资产

| 文件 | 来源 | 用途 |
|------|------|------|
| `m14_validation_report.md` | ECS 22:08 实跑 | 5 真指标 + 失败概览（自动生成）|
| `failed_cases.json` | ECS 22:08 实跑 | 3 条失败 case 详情 |
| `raw.json` | ECS 22:08 实跑 | 100 条 scenario 全量 LLM 原始响应（debug 用）|
| 本 README | 手工编写 | V5 完整复盘 + metric 缺陷闭环 + 结构发现 |

---

## 10. 结论

> **V5 metric 缺陷闭环（V4 P2 任务完成）**：
> - L1 修复：`evaluate_coverage` ref 无关键词时返 None，`run_validation.py` 跳过 None
> - L2 发现：12 个有效 case 全部在 ask_order_no 分支（agent UI 占位），0/12 暴露**结构性 metric 定义问题**
> - LLM 主指标（Resolver/RefundFlow/Tool）维持 V4 水平（96% / 96.7% / 100%）
> - 真幻觉率 1% → 0%（LLM 非确定性，本次未触发 M14-0070 伪单号）
>
> **诚实的下一步建议（P0 任务）**：
> 1. **real_corpus.json 加 expected=synthesize case**（如 "我的订单 ORD20260718003 收到了 7 天无理由退货" + order owned by user → 触发 synthesize 分支）
> 2. **确认 RefundFlow synthesize 分支正常输出政策文本**
> 3. **V6 重跑后用新 metric（只评 synthesize 分支）量化 KB 真实政策贡献**
>
> **不动业务代码 / 配置 / prompt / 数据库**。本批 commit 只动评测脚本。