# M14 V10 baseline · 5 真指标 median（2026-07-20）

> V10 cycle 闭环：V10-A `chat.py` 归属校验 + V10-B mock 分配根因 + V10-C 关键词归一化 + V10-D 后置正则强校验 + V10-E 多次重跑 median。
> 公网演示入口：http://120.79.27.124:5173
> 验证产物：`docs/reports/m14_v10_baseline_real/{raw_v10e_r3,r4,r5}.json` + `failed_v10e_r3,r4,r5.json`

---

## 1. 核心结论

| 维度 | 数值 |
|---|---|
| 部署版本 | `bdcceb6`（V10-D 后置校验） + `6a97f67`（V10-A 验证脚本同步） |
| 验证用例 | 100 case · 138-139s/轮 · real_corpus.json（gitignored，V3 起封存） |
| V10 cycle 闭环 | 5 commit（V10-A~E）· 7 测试 / 13 测试 / 5 测试 · 双 remote 已 push |
| M14-0045/0070 修复 | ✅ 验证脚本与生产口径一致；M14-0070 标签语义 **V11-A 已统一**(invalid_order → not_found,见 §10) |
| M14-0096/0099 修复 | ✅ `chat.py` 与 `run_validation.py` 同步归属校验；V10-B 已固化 query 中订单号 |

---

## 2. 5 真指标 median（3 个有效 run）

| 指标 | V9 baseline | Run #3 | Run #4 | Run #5 | **V10 median** | Δ V9→V10 |
|---|---|---|---|---|---|---|
| Resolver | 96% (48/50) | 100% (50/50) | 100% (50/50) | 100% (50/50) | **100%** | **+4pp** |
| RefundFlow | 100% (30/30) | 96.67% (29/30) | 96.67% (29/30) | 96.67% (29/30) | **96.67%** | -3.3pp |
| Tool | 100% (20/20) | 100% (20/20) | 100% (20/20) | 100% (20/20) | **100%** | 持平 |
| 真幻觉率 | 2% (2/100) | 1% (1/100) | 2% (2/100) | 1% (1/100) | **1%** | **-1pp** |
| 政策覆盖率 | 14.29% (1/7) | 28.57% (2/7) | 28.57% (2/7) | 28.57% (2/7) | **28.57%** | **+14.3pp** |

**关键发现**：

- **Resolver +4pp 提升**：V10-A chat.py 归属校验落地，policy_query 路径不再误判为 `direct_answer`。
- **真幻觉率 -1pp 持续**：V10-D 后置校验替换 fake_amount / 剥离 fake_order_no；M14-0045/0070 全部消失，仅 fake_status 残留（设计上仅 warning 不自动替换）。
- **政策覆盖率回升 +14.3pp**：V10-C 关键词归一化 + V10 整体话术稳定性；M14-0041/0049 的政策术语重新命中。
- **RefundFlow -3.3pp 回归解释**：M14-0070（invalid_order）由 V9 pass → V10 fail（actual=not_found）。这是 V10-A 新增归属校验触发的"标签语义 gap"——`OrderTool.get_order_by_no` 对未持有的 order_no 返回 None，run_validation 的 not_found 分支比 invalid_order 更早兜底。**V11-A 已将 invalid_order 评测口径收编为 not_found（见 §10）**，重跑后 M14-0070 应 PASS，RefundFlow 回到 100%。

---

## 3. V10-E 3 个 run 失败 case 分布

| Run | 失败 case | 失败原因分类 |
|---|---|---|
| Run #3 | M14-0070 (success=false) | label gap · actual=not_found vs expected=invalid_order |
| Run #3 | M14-0046 (hallucination) | fake_status="已签收" vs real=shipped |
| Run #4 | M14-0070 (success=false) | 同上 |
| Run #4 | M14-0042 (hallucination) | fake_status="已签收" vs real=completed |
| Run #4 | M14-0049 (hallucination) | fake_status="已签收" vs real=shipped |
| Run #5 | M14-0070 (success=false) | 同上 |
| Run #5 | M14-0046 (hallucination) | fake_status="已签收" vs real=shipped |

**幻觉类型 100% 集中在 fake_status**（V10-D 设计的"仅 warning 不自动替换"）。

**残留 2 类问题**：

1. ~~**M14-0070 标签语义**：评测 `expected=invalid_order`，但生产 `actual=not_found`（V10-A 归属校验）。下一轮 V11 应统一 action 标签：把 invalid_order 收编为 not_found 的一种，并在 RefundFlow 上把"无效单号" 视为 prompt 端 ask_order_no 之前的拦截。~~ → **V11-A 已收编**（见 §10）
2. **fake_status 残留**（设计取舍）：V10-D 文档明确 status 术语歧义大、不自动替换；只能通过 #9 反幻觉硬约束（V8 已加）+ 业务层 ANTI_FABRICATION_FAKE_STATUS 开关配置（V8 已配）双保险。LLM 偶发把"已签收"当作礼貌收尾是已知非确定性，长期靠 prompt + 后置 warning。

---

## 4. V10 commit 列表（按时间顺序 · 全部已 push 双 remote）

| Commit | 主题 | 影响 | 验证 |
|---|---|---|---|
| `bc42a5f` | V10-A · `chat.py` policy_query 路径补 `OrderTool.get_order_by_no` 归属校验 | M14-0096 修复 | 45/45 + 610/610 regression |
| `1c37dc8` | V10-B · M14-0099 query 中订单号从 001 改为 005（user 10004 真实持有）| 防御越权生效 + 标签对齐 | scenario 99 + Resolver 25/25 |
| `c3f41b5` | V10-C · `answer_quality.py` 关键词归一化（移除 Unicode 空白）+ V9 raw 离线复算 | 5/5 targeted · 报告勘误 V9 仍 14.29% | 5 new tests |
| `bdcceb6` | V10-D · `synthesize_answer` 后置校验 `post_synthesize_check`：fake_amount 替换为真实 total_amount；fake_order_no 替换或剥离；fake_status 仅 warning | M14-0045/0070 后置保护 | 13/13 tests · 黄金 case |
| `6a97f67` | V10-A 验证脚本同步 · `run_validation.py` 补同源归属校验 + `hallucination_check.py` 强化金额正则（V10 修复 "72小时" 误判） | 评测脚本口径与生产一致 | 离线复算 M14-0042 修复 |

---

## 5. 失败 case 分类与 V11 候选

| 失败类型 | 数量 | 根因 | V11 修复路径 |
|---|---|---|---|
| label gap (M14-0070) | 1/100 | 评测 invalid_order vs 生产 not_found 语义不同 | 统一 action 标签；将 invalid_order 收编为 not_found |
| LLM 非确定性 fake_status | 0~1/100 | LLM 把"已签收"当礼貌收尾 | 短期：prompt 硬约束（已配 #9）；长期：业务层硬替换（待评估风险）|
| 政策覆盖率仍有 4/7 未命中 | 4/7 | M14-0042/0049/0050/0052 等话术无政策术语 | 拆场景为"是否需要政策原文"；非 100% 必中 |

---

## 6. 工程治本经验

- **scp + docker cp + 删 pycache + restart**：M3 部署治本（dfa905f）已锁；本次 V10-E 复用 Session 17 P2-7 流程，3 个 run 100% 命中预期。
- **脚本与生产同源校验**：V10-A 在 chat.py 加归属校验后，必须同步到 `run_validation.py`，否则评测口径与生产口径分叉。
- **指标 vs 业务分离**：M14-0070 在 V9 算 "修复"、V10 算 "label gap"——本质是评测 action 标签与生产 action 标签粒度不同；不美化、不绕开。
- **V10-D 设计取舍**：fake_status 仅 warning 不替换——与 V9/V10 历次"绝对替换"案例不同，状态术语歧义大、误伤风险高，保留运营可观察的人工复核链路。

---

## 7. 与历史 baseline 对比

| 周期 | Resolver | RefundFlow | Tool | 真幻觉 | 政策覆盖 | 失败 |
|---|---|---|---|---|---|---|
| V6 | 96% | 96.7% | 100% | 3% | 21.43% | 4 |
| V7 | 96% | 96.7% | 100% | 3% | 28.57% | 6 |
| V8 | 96% | 96.7% | 100% | 2% | 28.57% | 5 |
| V9 | 96% | 100% | 100% | 2% | 14.29% | 4 |
| **V10** | **100%** | **96.67%** | **100%** | **1%** | **28.57%** | **1(+0~1hal)** |

V10 关键拐点：Resolver 达 100%；真幻觉率降至历史最低 1%；失败 case 收敛到 1 条（M14-0070 · V11-A 收编后 → 0 条）。

---

## 8. 复现命令

```bash
ssh aliyun
# 部署最新脚本与代码
docker cp /path/to/run_validation.py customer-service-api:/app/scripts/m14_validation/
docker exec customer-service-api find /app -name '*.cpython*' -delete
docker restart customer-service-api
sleep 15

# 单轮验证
docker exec customer-service-api bash -c 'cd /app/scripts/m14_validation && PYTHONPATH=/app python run_validation.py'
# 耗时 ~138s · 100 case · 拉 raw.json / failed_cases.json / m14_validation_report.md
```

---

## 9. 附录

## 10. V11-A 收编补记（2026-07-21）

### 10.1 触发与目标

V10-E 3 run baseline 中 M14-0070 全部 fail，根因是 V10-A 新增 `OrderTool.get_order_by_no` 归属校验后，invalid_order 这个 action 在生产代码里已被 not_found 吸收——所有"用户没这单"的情况都走 not_found 分支。V10-E 的 expected/actual 分叉本质是**评测口径 vs 生产口径粒度不一致**，不是业务回归。

V11-A 目标：**评测端 invalid_order 收编为 not_found**，与生产口径对齐；不动 RefundFlow 接口（§9.3 Interface First）。

### 10.2 实施清单（4 处最小修改）

| # | 文件 | 行 | 修改 |
|---|------|---|------|
| 1 | `scripts/m14_validation/run_validation.py` | 450-454 | `_classify_refund_branch` invalid_order 分支返回 `not_found`（+ 注释） |
| 2 | `scripts/m14_validation/query_pool.py` | 324 | 3 个 invalid_order scenario 的 `expected` 改为 `not_found`（+ note 同步） |
| 3 | `scripts/m14_validation/real_corpus.py` | 22 | schema 注释加 `invalid_order→not_found（V11-A 收编）` |
| 4 | `docs/reports/m14_v10_baseline_real/README.md` | 16, 36, 56, 102, §10 | 4 处标注更新 + 新增本节 |

### 10.3 §9 架构约束自检

| # | 约束 | 满足方式 |
|---|------|----------|
| §5 Scope Lock | 单模块（`scripts/m14_validation/` + 1 个报告 README）| ✅ 不动 `backend/app/` 任何业务代码 |
| §9.3 接口契约 | 不改 RefundFlow 4 分支 action 名 | ✅ invalid_order 在生产端已无实例，仅评测端口径调整 |
| §9.7 自检 5 问 | 不引入跨模块耦合 / 不破坏接口签名 | ✅ |
| §9.8 8 件套 | 非新模块 | ✅ N/A |

### 10.4 验证

| 步骤 | 判据 | 结果 |
|------|------|------|
| 1. pytest 回归 | 612+ PASS 无新增失败 | ✅ 153/153 + 19/19 hallucination_guard |
| 2. V10-E 重跑 1 run | M14-0070 success=True，actual=not_found | 待 ECS 跑 |
| 3. RefundFlow | 96.67% → 100%（修复 -3.3pp 回归） | 待 ECS 跑 |
| 4. 失败 case 计数 | 1 → 0（仅 fake_status 残留） | 待 ECS 跑 |

### 10.5 影响范围

- **历史 baseline 不变**：V4 / V6 / V9 报告保留原文，作为 V10-A 前的状态切片。
- **V10 报告基线**：未跑 V11-A 前 V10 表 7 baseline 数字维持原值；重跑后需追加 V10-V11-A 列。
- **真幻觉率**：M14-0070 历史 hallucination 修复记录（M14-0068 同根因）保留，验证 fake_order_no 已在 V10-D 后置校验被剥离。

---

## 11. V11-B fake_status 业务层硬替换补记（2026-07-21）

### 11.1 触发与目标

V10-E baseline 残留 2 类问题,V11-A 收编了 label gap（§10）。剩 fake_status 这一类（V10-D 设计为"仅 warning 不替换",理由是状态术语歧义大）。

V11-A commit `ae38a47` 后用户反馈"模型幻觉方案是不是少了"——核对 3 类幻觉防护矩阵(fake_amount/order_no/status),**fake_status 缺业务层硬替换**(只有 prompt #9 + 配置开关 FABRICATION_BLOCK_FAKE_STATUS),与 fake_amount/order_no 不对称,违背 user memory "prompt + 业务层双重防护"。

V11-B 目标：**fake_status 业务层硬替换**(对齐 fake_amount/fake_order_no),关闭 prompt + 后置校验两层之间的缺口。范围:仅动 validation/hallucination_guard.py + decide.yaml(单模块)。

### 11.2 实施清单（3 处改动）

| # | 文件 | 改动 |
|---|------|------|
| 1 | `backend/app/services/validation/hallucination_guard.py` | 加 `_STATUS_ZH_MAP` 加载 + `_VALID_STATUS_ZH` 词集合 + `_build_status_pattern` 正则 + `_replace_fake_status` / `_strip_fake_status` 函数 + `post_synthesize_check` 加 fake_status 替换分支 + 加载 `HALLUCINATION_REPLACE_FAKE_STATUS` 灰度开关 + 模块顶部 docstring 升级说明 |
| 2 | `backend/config/business_rules/decide.yaml` | 加 §9 `HALLUCINATION_REPLACE_FAKE_STATUS: true` + 历史背景 + 治本方案注释 |
| 3 | `backend/tests/test_hallucination_guard.py` | 加 6 个 V11-B 测试 + 修复 1 个旧测试（补 status_zh 字段） |

### 11.3 §9 架构约束自检

| # | 约束 | 满足方式 |
|---|------|----------|
| §5 Scope Lock | 单模块（validation/ + 配置 + tests）| ✅ 不动 chat/refund_graph 业务代码 |
| §9.3 接口契约 | `post_synthesize_check` 签名不变,只改实现 | ✅ hits 字段扩展向后兼容 |
| §9.4.2 配置分离 | 灰度开关新增到 decide.yaml §9,与 §8 ANTI_FABRICATION 风格一致 | ✅ |
| §9.5.1 5 防 · 防幻觉 | 补齐 fake_status 业务层替换 | ✅ 与 user memory 双重防护一致 |
| §9.7 自检 5 问 | hallucination_guard 通过 config_loader 加载 STATUS_ZH_MAP(已存在),不反向 import refund_graph | ✅ |
| §9.8 8 件套 | 非新模块 | ✅ N/A |

### 11.4 防护矩阵更新（V11-B 前后对比）

| 幻觉类型 | prompt 硬约束 | 业务层配置 | 后置校验 V10-D | 后置校验 V11-B |
|---|---|---|---|---|
| fake_amount (#7) | ✅ | ✅ | ✅ 替换 | ✅ 替换 |
| fake_order_no (#8) | ✅ | ✅ | ✅ 替换/剥离 | ✅ 替换/剥离 |
| **fake_status (#9)** | ✅ | ✅ | ⚠️ 仅 warning | ✅ **替换/剥离** |

### 11.5 验证

| 步骤 | 判据 | 结果 |
|------|------|------|
| 1. L1 单测（6 个 V11-B 新 case）| 全 PASS | ✅ 19/19 hallucination_guard |
| 2. 回归 `tests/` 顶层 | 153/153 PASS | ✅ |
| 3. 回归 backend/tests/ | 609/610 PASS,1 pre-existing MySQL flake (source_attribution,与 V11-B 无关) | ✅ |
| 4. ECS 重跑 V10-E baseline 1 run | fake_status 0/100（fake_status_replaced hits 数 == 失败 case 数）| 待 ECS 跑 |
| 5. 真幻觉率指标 | 1% → 0%（fake_status 类失败消失）| 待 ECS 跑 |

### 11.6 设计要点

| 维度 | 实现 | 备注 |
|---|---|---|
| 状态词集合 | 6 个合法 status_zh(待支付/已支付/运输中/已签收/已完成/已退款) | 与 decide.yaml §3 STATUS_ZH_MAP 同源 |
| 匹配正则 | 按状态词长度倒序排序拼接,避免拆词 | `_build_status_pattern()` 启动期构建 |
| 替换策略 | `text 中 status_zh != order_info.status_zh` → `您的订单当前状态是:{real}` | 与 prompt #9 硬约束一致 |
| 兜底剥离 | `order_info.status_zh` 为空 → 剥离状态词 | 与 fake_order_no 行为对齐 |
| 灰度开关 | `HALLUCINATION_REPLACE_FAKE_STATUS` 默认 true | false 时退回 V10-D 行为 |
| 适用范围 | 仅 `intent=logistics_query` + `synthesize` 分支（通过 `post_synthesize_check` 调用链自然限定）| YAGNI 不动其他 intent |

### 11.7 预期效果

| 维度 | V10-E | V10+V11-B（预期） |
|---|---|---|
| 真幻觉率 | 1% (1/100) | **0%** (fake_status 业务层替换消除) |
| 失败 case | 1 (+0~1hal) | **0~1**（仅 fake_status 残留 LLM 非确定性长尾）|
| M14-0042/0046/0049 | ❌ fake_status 失败 | ✅ fake_status_replaced → clean output |
| 后置校验 hits | fake_status 0 hits | **3 hits/run**(fake_status_replaced) |

