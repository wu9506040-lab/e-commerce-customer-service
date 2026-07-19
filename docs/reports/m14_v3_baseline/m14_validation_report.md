# M14 业务闭环真实话术验证报告

> **整改说明 (2026-07-18)**：按用户反馈『模拟的业务和数据要有依据合理』，
> V2 报告改用公开话术合集（道客巴巴/帮客服/搜狐/京东/淘宝/拼多多 帮助中心）作为真实测试场景，
> 4 旧伪指标替换为 5 真指标（决策准确率/分支准确率/工具准确率/真幻觉率/政策覆盖率）。

> 验证时间: 2026-07-19 12:27:56  
> 验证耗时: 49.0s  
> 总 scenario: 100  
> 失败 case: 20  
> 数据源: `scripts/m14_validation/data/real_corpus.json`（100 条真实话术）

## 1. 5 真指标（V2）

| 指标 | 值 | 公式 | 含义 |
|------|----|----|------|
| **Resolver 决策准确率** | **86.0%** | 43/50 | 真实 action == 期望 action |
| **RefundFlow 分支准确率** | **60.0%** | 18/30 | 真实分支 == 期望分支 |
| **Tool 调用准确率** | **100.0%** | 20/20 | Tool 返回成功 |
| **真幻觉率** ⬇️ | **1.0%** | 1/100 | Agent 胡编实体 case / 总 case |
| **政策覆盖率** ⬆️ | **14.8%** | 4.0/27 | ref 关键词在 Agent 输出中出现率 |

## 2. Resolver 4 Actions 分布

| Action | 触发次数 | 占比 |
|--------|---------|------|
| DIRECT_ANSWER | 23 | 46.0% |
| SHOW_PICKER | 21 | 42.0% |
| ASK_LOGIN_OR_LIST | 0 | 0.0% |
| NOT_FOUND | 3 | 6.0% |
| ASK_LOGIN | 3 | 6.0% |

## 3. RefundFlow 4 分支分布

| 分支 | 触发次数 | 占比 |
|------|---------|------|
| synthesize | 0 | 0.0% |
| escalate | 0 | 0.0% |
| ask_order_no | 15 | 50.0% |
| invalid_order | 3 | 10.0% |

## 4. 真幻觉校验明细

| Case | 类型 | 抽取实体 | 详情 |
|------|------|---------|------|
| M14-0070 () | fake_order_no | ORD99999999999 | 合法选项: ['ORD20260718002', 'ORD20260718003', 'ORD20260718004'] |

## 5. 失败 Case 概览（20 条）

| ID | Corpus | Expected | Actual | 失败原因 |
|----|--------|----------|--------|---------|
| M14-0027 | RC013 | show_picker | direct_answer | 预期 show_picker，实际 direct_answer |
| M14-0031 | RC017 | show_picker | direct_answer | 预期 show_picker，实际 direct_answer |
| M14-0032 | RC018 | show_picker | direct_answer | 预期 show_picker，实际 direct_answer |
| M14-0033 | RC019 | show_picker | direct_answer | 预期 show_picker，实际 direct_answer |
| M14-0034 | RC020 | show_picker | direct_answer | 预期 show_picker，实际 direct_answer |
| M14-0051 | RC005 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0052 | RC015 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0053 | RC056 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0054 | RC069 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0055 | RC077 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0056 | RC095 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0057 | RC097 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0058 | RC012 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0059 | RC033 | escalate | unknown | 预期 escalate，实际 unknown |
| M14-0060 | RC051 | escalate | unknown | 预期 escalate，实际 unknown |

## 6. 真实场景展示（5 条代表案例 · 来自公开话术合集）

> 以下场景全部来自 `data/real_corpus.json`（公开话术整理），每条展示：
> 用户真实 query（来源标注） + 真实客服 reference + Agent 实际 final_answer + 校验结果。

### 场景 1: refund 类（M14-0041 · RC001）

**用户 query**（来源: 帮客服《电商常用退款话术快捷回复》 · 平台: 通用）：
> 我在网上买的衣服还没收到，但现在不想要了，能退款吗？

**真实客服回复模板**（reference_answer）：
> 您好，您的快递已于昨日寄出，我们已为您发起快递拦截，待快递反馈拦截结果后，我们会为您处理售后，预计24小时可给您反馈拦截结果，请您耐心等待~...

**Agent 实际输出**（final_answer，长度=19）：
> 您有 3 个订单，请选择要退款的订单：...

**校验结果**：
- 决策: 期望 `ask_order_no` → 实际 `ask_order_no` ✅
- 政策覆盖率: **0.0%** (覆盖 0/1 关键词)
- 缺失关键词: `24小时`
- 真幻觉: ✅ 无

### 场景 2: logistics 类（M14-0059 · RC033）

**用户 query**（来源: 搜狐《快递物流回复客服话术》 · 平台: 通用）：
> 你们再不派送我就投诉 12315 和 12305！

**真实客服回复模板**（reference_answer）：
> 您的心情，我是很理解的，我们一直也在为您催促快递呢。但我们是商家客服，并不是快递方。快递公司一般都有自己的一套运输轨迹，我们商家也没法给您亲自送快递。我们目前能为您做的，就是联系快递催促加急。请您相信我，我们商家其实比您更着急，更想您早点收到包裹呢。...

**Agent 实际输出**（final_answer，长度=19）：
> 您有 5 个订单，请选择要退款的订单：...

**校验结果**：
- 决策: 期望 `escalate` → 实际 `unknown` ❌
- 政策覆盖率: **0.0%** (覆盖 0/2 关键词)
- 缺失关键词: `快递, 催促`
- 真幻觉: ✅ 无

### 场景 3: escalate 类（M14-0060 · RC051）

**用户 query**（来源: 综合（基于真实 case 截图 / 微博） · 平台: 通用）：
> 我要投诉！你们这是什么破服务！

**真实客服回复模板**（reference_answer）：
> 非常抱歉给您带来不愉快的体验，您的诉求我们已记录，会有专员尽快联系您处理，请您保持电话畅通。...

**Agent 实际输出**（final_answer，长度=19）：
> 您有 3 个订单，请选择要退款的订单：...

**校验结果**：
- 决策: 期望 `escalate` → 实际 `unknown` ❌
- 政策覆盖率: **0.0%** (覆盖 0/3 关键词)
- 缺失关键词: `专员, 处理, 联系`
- 真幻觉: ✅ 无

---

_本报告由 `scripts/m14_validation/run_validation.py` 自动生成（V2）_