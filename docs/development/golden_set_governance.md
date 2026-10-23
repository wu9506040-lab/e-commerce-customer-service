# 评测集治理方案：黄金集冻结 + 分层统计 + badcase 回流（V21-EV）

> 2026-10-24 · 针对 eval 体系的三条治理规则落地设计
> 一句话：**数据切分的目的是"可对比、可定位、防回归"，不是"把数据分给训练和测试"**

---

## 0. 现状对照（差距分析）

| 治理规则 | 现状 | 缺口 |
|---|---|---|
| 黄金集冻结 | 330 题 hit@k + 60 题话术 + 20 组多轮，**但无冻结机制**——60 题曾发生过"3 处 expected 对齐答案措辞"的篡改事故 | ❌ 无版本化、无防篡改校验 |
| 分层统计 | 330 题有 source 字段；60 题有售前/售中/售后；多轮有 4 类场景 | ⚠️ 有维度没出**分层矩阵**（每层各自出指标） |
| badcase 回流 | badcase 池存在（badcase_service + merge 脚本） | ⚠️ 与黄金集**没有晋级闭环**（修好→验证→入集 流程未定义） |

三条规则全部成立，且第 1 条正是踩过的坑——**60 题"改 expected"事故就是"边测边改"的反面教材**，这次治理直接把它用机制锁死。

---

## 1. 黄金集冻结（Golden Set Freezing）

### 1.1 目录与版本化
```
data/evalsets/
├── golden/
│   ├── golden_v1.0.json          # 当前冻结版（评测只认这个）
│   ├── golden_v1.1.json          # 扩容后的下一版（旧版保留可跑对比）
│   └── MANIFEST.json             # 版本清单 + 校验和
├── badcase_pool/
│   ├── pending/                  # 回流的失败案例（待修清单）
│   └── promoted/                 # 已修复并晋级的历史
└── CHANGELOG.md                  # 每次增删的记录：为什么、谁批、影响多少题
```

### 1.2 MANIFEST 防篡改（核心机制）
```json
{
  "version": "v1.0",
  "frozen_at": "2026-10-24T10:00:00",
  "total": 450,
  "by_layer": {"hitk": 330, "verbal": 60, "multiturn_scenarios": 20},
  "cases_sha256": {
    "MT01.turn2.query": "a3f...",     // 每题 query+expected 的联合哈希
    "h067.query": "b7e...",
    "...": "..."
  },
  "rule": "任何 cases_sha256 与本清单不符 = 评测集被改动，评测结果作废"
}
```
- **改 expected → hash 变 → 脚本直接拒绝跑**（除非走"发版"流程更新 MANIFEST + CHANGELOG 记录）
- 这把"60 题改 expected"从事故变成**机制上不可能静默发生**

### 1.3 发版流程（黄金集怎么"长大"）
```
badcase 修复 → 三套回归全绿 → 提案（CHANGELOG 写明为什么收）
→ 新版本 golden_v1.x（新题只进新版，旧版保留）
→ 对比跑：v1.0 与 v1.1 各跑一遍，证明新增没有稀释历史可比性
```
- 每次**评测用同一份**；跨版本对比时两份都跑
- 增删记录永久保留——"黄金集逐步长大但每一步可追溯"

---

## 2. 分层统计（场景 × 难度矩阵）

### 2.1 每题补 metadata（schema 升级，向后兼容）
```json
{
  "query": "...",
  "expected": {...},
  "meta": {
    "scenario": "refund_process",      // 10 类场景（见 2.2）
    "difficulty": "medium",            // easy / medium / hard
    "source": "seed_2026-10",          // 来源（seed / badcase_promoted / manual）
    "added_in": "v1.0"
  }
}
```

### 2.2 十类场景（贴电商客服真实分布）
1. policy_faq（政策问答）2. product_params（商品参数）3. order_logistics（订单物流）
4. refund_process（退款流程）5. bargain（议价）6. after_sales（售后/换货）
7. multi_turn_coref（多轮指代）8. ambiguous_intent（意图模糊）
9. adversarial（对抗/诱导：要求报错价、越权查单）10. chitchat_guard（闲聊护栏）

### 2.3 分层矩阵报告
`scripts/eval_report.py`：输出 场景 × 指标 矩阵（每行单独算，不是总平均）：
```
| 场景 | 题数 | hit@3 | 话术通过率 | 转人工率 |
|---|---|---|---|---|
| refund_process | 80 | 0.91 | 0.88 | 0.10 |
| adversarial | 30 | — | 0.60 ⚠️ | 0.35 |   ← 一眼定位拖后腿
```
- **可定位**："哪一类拖后腿"直接看矩阵最差行
- 难度档同理出一张表

---

## 3. badcase 池闭环（回流 → 修复 → 晋级）

```
线上/评测失败 → badcase_service 入池（现有机制，补 meta：场景/难度标签）
→ 每周（或每批改动后）从池里取一批修复
→ 修复验证：池内该题重跑绿 + 三套回归全绿
→ 评审通过 → 代表性案例"晋级"进黄金集下一版本（带 source=badcase_promoted）
→ 非代表性的留在 promoted 归档
```
- **纪律**：badcase 不直接进黄金集（防"用刚修的题考自己"）；必须隔一个版本
- 晋级配额建议：单次发版新增 ≤ 总量 10%（防黄金集被修复偏向性污染）

---

## 4. 工作量与交付

| 项 | 内容 | 量 |
|---|---|---|
| MANIFEST + 校验 | evalset schema 升级 + hash 校验脚本（评测入口强制检查） | 1 天 |
| 分层矩阵 | meta 标签补齐（330 题打标）+ eval_report.py | 1 天 |
| badcase 闭环 | 晋级流程 + 配额校验 + CHANGELOG | 1 天 |
| 扩容 | 450 → 600（按场景矩阵的短板补题，adversarial 优先） | 1 天 |

## 5. 面试话术（这套治理本身是加分项）

> 「我的评测集是**治理过的**：黄金集冻结加版本化，每题 query 和 expected 有联合哈希进 MANIFEST——改评测集本身会破坏校验和，因为踩过'为了分数好看改 expected'的坑，所以把它变成机制上不可能。统计是场景 × 难度的分层矩阵，不是总平均——拖后腿的类一眼可见。badcase 单独成池，修复验证过了才隔版本晋级进黄金集，单次扩量不超过 10%——**黄金集是长大的，但每次评测用同一份，保证可比、可定位、防回归**。」
