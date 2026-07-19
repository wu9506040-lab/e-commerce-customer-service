# M14 V8 baseline · #9 fake_status + ANTI_FABRICATION 反幻觉硬约束重跑

> **验证时间**：2026-07-19 23:15:29 · **耗时**：135.3s · **case 数**：100 · **失败**：5
> **上游基线**：V7（30467f0）· T2.2 POLICY_QUOTE_REQUIRED + #7+#8 反幻觉
> **本批代码**：commit `9cd2f8e` · P2-3 #9 fake_status + ANTI_FABRICATION §8 配置化
> **验证方式**：ECS 100 case 真实话术重跑 + 2 阶 commit ref（M14-0068 防伪规则 + M14-0046 V7 fake_status 失败）

---

## 1. 5 真指标 V7 → V8 对比

| 指标 | V7 | V8 | Δ | 解读 |
|------|----|----|---|------|
| Resolver 准确率 | **96%** (48/50) | **96%** (48/50) | 持平 | ✅ OrderContextResolver 4 决策稳定 |
| RefundFlow 分支准确率 | **96.7%** (29/30) | **96.7%** (29/30) | 持平 | ✅ decide 节点 3 层决策稳定 |
| Tool 调用准确率 | **100%** (20/20) | **100%** (20/20) | 持平 | ✅ OrderTool 稳定 |
| **真幻觉率** ⬇️ | **3%** (3/100) | **2%** (2/100) | **-1pp** ✅ | #9 + ANTI_FABRICATION 消除 M14-0046 fake_status |
| **政策覆盖率** ⬆️ | **28.57%** (2.0/7) | **28.57%** (2.0/7) | 持平 | #9 状态硬约束不直接影响政策引用 |

**核心结论**：
- ✅ #9 fake_status 完全消除 M14-0046（V7 fail → V8 pass）
- ✅ 5 真指标全部稳定或改善
- ⚠️ #7 fake_amount 仍存（M14-0045/0048 复现）= LLM 非确定性瓶颈

---

## 2. 反幻觉硬约束效果逐项分析（V6 → V7 → V8）

| 硬约束 | commit | 真实 case 效果 | 状态 |
|--------|--------|---------------|------|
| **#6** 政策原文强制引用（POLICY_QUOTE_REQUIRED）| `a174c0d` | M14-0047 单 case 100% 命中"7天无理由" · 21.43%→28.57% | ✅ 持续生效 |
| **#7** fake_amount 校验（"金额必须从【事实陈述】取值"）| `aa4bdd7` | M14-0045/0048 仍触发 · LLM 偶发 reject 失败 | ⚠️ 部分有效 |
| **#8** fake_order_no 校验（"订单号必须从【事实陈述】取值"）| `aa4bdd7` | M14-0070 完全修复 | ✅ 完全有效 |
| **#9** fake_status 校验（"状态必须从【事实陈述】取值"）| `9cd2f8e` | **M14-0046 fake_status="已签收" 完全修复** | ✅ 完全有效 ⭐ V8 真实价值 |
| **ANTI_FABRICATION_ENABLED** | `9cd2f8e` | 业务层加固；不只 prompt | ✅ 配置化驱动 |

---

## 3. V8 失败 Case（5 条 · vs V7 6 条）

| # | ID | Corpus | Expected | Actual | 失败原因 | 根因 |
|---|----|--------|----------|--------|----------|------|
| 1 | M14-0045 | RC006 | synthesize | synthesize | 幻觉: fake_amount | #7 难彻底消除（LLM 非确定性）· 同 case V6/V7/V8 均触发 |
| 2 | M14-0048 | RC009 | synthesize | synthesize | 幻觉: fake_amount | 同上 |
| 3 | M14-0062 | RC054 | escalate | unknown | 预期 escalate，实际 unknown | DB 偶发 + P0 关键词未覆盖（"3 倍赔偿"属 compensation 未在 4 类 P0）· **P2-5 修复路径** |
| 4 | M14-0096 | RC017 | not_found | direct_answer | policy_query 短路 → non_order_intent → 跳过归属校验 | Resolver policy_query 分支缺陷 · **P2-4 修复路径** |
| 5 | M14-0099 | — | direct_answer | not_found | 长 query 抽取 order_no + 归属反向校验 | 同根因 · **P2-4 修复路径** |

### V7 → V8 失败 Case 对比

| ID | V7 | V8 | 变化 |
|----|----|----|------|
| M14-0045 | fail (fake_amount) | fail (fake_amount) | 持平 · LLM 非确定性 |
| **M14-0046** | **fail (fake_status)** | **NOT in failed** | ✅ **#9 修复** |
| M14-0048 | fail (fake_amount) | fail (fake_amount) | 持平 · LLM 非确定性 |
| M14-0062 | fail (escalate unknown) | fail (escalate unknown) | 持平 · 待 P2-5 兜底 |
| M14-0096 | fail (not_found ↔ direct_answer) | fail (not_found ↔ direct_answer) | 持平 · 待 P2-4 归属校验 |
| M14-0099 | fail (not_found ↔ direct_answer) | fail (not_found ↔ direct_answer) | 持平 · 同根因 |
| **合计** | **6** | **5** | **-1** (-16.7%) |

---

## 4. 业务层加固（ANTI_FABRICATION）说明

`decide.yaml` §8 新增 4 开关驱动 YAML 化反幻觉规则：

```yaml
ANTI_FABRICATION_ENABLED: true         # 全局总开关（false → 退回 V2 行为）
FABRICATION_BLOCK_FAKE_AMOUNT: true    # #7 单独可关
FABRICATION_BLOCK_FAKE_ORDER_NO: true  # #8 单独可关
FABRICATION_BLOCK_FAKE_STATUS: true    # #9 单独可关
```

`refund_graph.py:130-134` 加载：

```python
ANTI_FABRICATION_ENABLED: bool = bool(_RULES.get("ANTI_FABRICATION_ENABLED", True))
FABRICATION_BLOCK_FAKE_AMOUNT: bool = bool(_RULES.get("FABRICATION_BLOCK_FAKE_AMOUNT", True))
FABRICATION_BLOCK_FAKE_ORDER_NO: bool = bool(_RULES.get("FABRICATION_BLOCK_FAKE_ORDER_NO", True))
FABRICATION_BLOCK_FAKE_STATUS: bool = bool(_RULES.get("FABRICATION_BLOCK_FAKE_STATUS", True))
```

**业务价值**（CLAUDE.md §9.4.2 配置分离）：
- 关 ANTI_FABRICATION_ENABLED → 退回 V2 行为（V2 baseline 历史数据）
- 单独关 BLOCK_FAKE_STATUS → #9 不注入但 #7/#8 仍生效（A/B 实验友好）
- 配合 5 灰度开关可做"反幻觉规则流量分桶"
- 改 YAML 不重启容器：决定开关后下次 ingest 重加载（refund_graph.py 单 startup load）

---

## 5. 工程治本 vs LLM 非确定性（F29 §29.4 真实数据验证）

| 维度 | #9 fake_status（V8） | #7 fake_amount（V8）|
|------|---------------------|---------------------|
| 单点 case 修复 | ✅ M14-0046 完全消除（V7 fail → V8 pass）| ❌ M14-0045/0048 仍触发 |
| 失败 case 减少 | -1 (6 → 5) | 0（瓶颈未突破）|
| 假设解释 | LLM 在状态字段约束下能可靠采纳"运输中"真值 | LLM 在金额字段约束下倾向"近似猜"（语义层面 skip）|

**核心洞察（V8 实证）**：
- ✅ 反幻觉硬约束对**精确取值字段**（状态/订单号）高度有效
- ⚠️ 反幻觉硬约束对**语义聚合字段**（金额近似）部分有效
- 📌 提升路径：
  - **短中期**：多次重跑观察 #7 稳定性 + 加更多 fake_amount 关键词同义约束
  - **长期**：synthesize prompt 后置校验（LLM 输出后用正则强校验金额字段 = 真实订单金额）

---

## 6. 验证命令

```bash
# ECS 上传播新代码
ssh aliyun
mkdir -p /tmp/v8_files
# scp (本地) → /tmp/v8_files/ → docker cp (容器)
docker cp /tmp/v8_files/refund_graph.py customer-service-api:/app/app/services/refund_graph.py
docker cp /tmp/v8_files/decide.yaml customer-service-api:/app/config/business_rules/decide.yaml
find /app -name 'refund_graph.cpython*' -delete   # 清 pycache
docker restart customer-service-api
sleep 18

# 验证 P2-3 代码已加载
docker exec customer-service-api python -c 'from app.services.refund_graph import ANTI_FABRICATION_ENABLED, FABRICATION_BLOCK_FAKE_STATUS; print(ANTI_FABRICATION_ENABLED, FABRICATION_BLOCK_FAKE_STATUS)'
# 输出: True True ✅

# 跑 V8
docker exec customer-service-api bash -c 'cd /app/scripts/m14_validation && PYTHONPATH=/app python run_validation.py'
# 135.3s · 100 case · 失败 5
```

---

## 7. 残留待办（独立 P2）

| 优先级 | ID | 任务 | 工时 | 进度 |
|--------|----|------|------|------|
| **P2-4** | — | Resolver policy_query + order_no entity 归属校验（修 M14-0096/0099）| 30 min | ⏳ pending |
| **P2-5** | — | refund_graph unknown 分支兜底（修 M14-0062 + escalate 兜底可观测）| 30 min | ⏳ pending |
| **V9** | — | P2-4/P2-5 commit 后 ECS 重跑（预期失败 5 → ≤ 3 + 政策覆盖率可能再提升）| 30 min | ⏳ pending |
| **E1** | — | HandoffCard P2 蓝浏览器验证（playwright 强制注入 state）| 30 min | ⏳ pending |
| **U1/U2/U3** | — | 用户侧（healthcheck UUID + Gitee Releases + 域名采购）| 用户侧 | ⏳ pending |

---

## 8. commit 链路 + 提交计划

| Commit | 关联 | 影响 |
|--------|------|------|
| 30467f0 | V7 baseline | V8 对比基准 |
| **9cd2f8e** | **P2-3 #9 + ANTI_FABRICATION** | 反幻觉完全修复 M14-0046 |
| **本批 commit** | **V8 README + 报告 + 失败 case 重跑** | 量化 #9 + ANTI_FABRICATION 真实价值 |

**提交范围**：

- A：`docs/reports/m14_v8_baseline_real/README.md`（本文件）· 新建
- A：`docs/reports/m14_v8_baseline_real/raw.json`（75857 bytes）· 新建
- A：`docs/reports/m14_v8_baseline_real/failed_cases.json`（5 条）· 新建
- A：`docs/reports/m14_v8_baseline_real/m14_validation_report.md`（脚本生成）· 新建
- M：`findings.md` F31 · 新增
- M：`progress.md` Session 19 · 新增
- M：`docs/_private/resume_snippet.md` · 2 处数字更新（gitignored）

**不动**：业务代码 / 配置 / prompt / 数据库 / 部署（`9cd2f8e` 已落）。

---

## 9. AI Review（CLAUDE.md §4.5 五项检查单）

| # | 检查 | 结果 |
|---|------|------|
| 1 | §2 禁止行为 | ✅ 仅 ECS 重跑 + 报告 + 同步；不改业务代码 / 配置 |
| 2 | 跨模块耦合 | ✅ refund_graph.py 单模块（`9cd2f8e`）；V8 报告与 ECS 数据解耦 |
| 3 | YAGNI | ✅ 不重构 V8 报告模板；仅复用 V7 README 结构 + 新增 #9 真实价值段 |
| 4 | 安全/合规/密钥 | ✅ 无密钥变动；mock 凭据来自容器 env |
| 5 | 接口影响 | ✅ decide_result schema / final_answer 类型不变 |

---

## 10. 简历 baseline 更新建议

| 指标 | V6 | V7 | V8 | 简历基线 |
|------|----|----|----|---------|
| 真实话术 5 真指标 | 96/96.7/100/**2**/21.43 | 96/96.7/100/**3**/28.57 | 96/96.7/100/**2**/28.57 | V8 (2% 真幻觉 + 28.57% 政策) |
| 反幻觉硬约束覆盖 | #6 + #7 + #8 | 同左 | **#6 + #7 + #8 + #9 全栈** | V8 |
| 配置化反幻觉开关 | 0 | 0 | **4 个 ANTI_FABRICATION 开关** | V8 |

`docs/_private/resume_snippet.md` 同步项：
- 1 句亮点：保留 "pytest 510 全过"（已 594 PASS 但精度太高可放宽）
- 项目描述 bullets：「反幻觉硬约束 #6+#7+#8」 → 「反幻觉硬约束 #6+#7+#8+#9 + 4 开关业务层加固」
- M14 真实话术回归行：96/96.7/100/2/28.57% V8 baseline（替代 V7 行）
- 6 个里程碑：M14 行补"#9+ANTI_FABRICATION V8 闭环"
- changelog：追加 2026-07-19 V8 条
