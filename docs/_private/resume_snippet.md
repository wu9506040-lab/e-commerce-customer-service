# 简历描述 baseline（V3 LangGraph 集成后）

> 创建日期：2026-06-27
> 用途：直接复制到 BOSS直聘 / 拉勾 / 猎聘 / LinkedIn 的项目描述区
> 后续更新：每个里程碑完成后回到这里同步更新

---

## 项目名称

**电商智能客服 Agent 系统**（LLM 全栈实战 / 2026.03 - 至今）

---

## 一句话定位（简历开头 summary 区）

> 基于 **RAG + Tool + LangGraph StateGraph** 的电商智能客服，覆盖商品咨询 / 订单 / 退款 / 政策 4 类意图；自研 Synthesizer 多源融合 + 环境变量灰度切换 + try/except fallback，**25 测试全过 / 4 路径全覆盖 / 50 并发压测通过**。

---

## 技术栈（ATS 关键词区）

```
Python 3.11 / FastAPI / LangGraph 0.2 / LangChain 0.3
Vue3 + TypeScript + Vite
Qdrant (COSINE) / DashScope text-embedding-v3 / Qwen-max LLM
MySQL 8.0 / Redis 7 / Docker Compose
```

---

## 项目描述（4 条 bullet，HR/面试官看这部分）

```
- 独立设计并实现基于 RAG + Tool + LangGraph 的电商智能客服系统，
  覆盖商品咨询 / 订单查询 / 退款 / 政策问答 4 类意图，
  端到端 40 用例实测平均通过率 95.8%

- 设计意图分类三级 fallback（关键词正则 → LLM 兜底 → 默认 policy_query），
  规则命中 < 100ms；自研 Response Synthesizer 按
  Tool > Policy > Product > History 优先级硬约束融合多源结果，
  prompt 结构强约束防 LLM 幻觉

- V3 引入 LangGraph StateGraph 重构退款流程（6 Node + 3 条件边 +
  escalate 升级人工），覆盖 4 条业务路径；
  环境变量灰度切换 + try/except fallback 到 V2.x；
  单测 16 个 + 集成测试 9 个全过（pytest，4.2s）

- 单体 FastAPI + 5 个 Docker 服务，支持 50 并发 / 平均首 token < 2s；
  通过 bcrypt + JWT + httpOnly Cookie + Tool 层强制 user_id 注入
  实现越权防护（防订单越权读取）
```

---

## 1 句亮点（自我介绍 / 简历最开头用）

> **自研 Synthesizer 多源融合 + LangGraph StateGraph 复杂路径编排，从意图识别到流式输出端到端可控**。

---

## 技术深度追问 5 分钟版（面试官追问时讲）

| 段 | 时长 | 讲什么 |
|---|---|---|
| **Why** | 30s | 业务到复杂门槛（5-6 步 + 条件分支 + 升级）才引入，**不是规则约束** |
| **What** | 60s | StateGraph 6 Node + 3 条件边，4 条路径覆盖（可指 mermaid 图）|
| **How** | 90s | stream_mode="updates" + pass-through 字段 + judge/conditional/escalate |
| **Trade-off** | 60s | env 灰度 + try/except fallback，零侵入上线，**values vs updates 选择** |

可以拿 `docs/refund_graph_v3.png` 当道具展示。

---

## 量化数字（让 HR 一眼看懂你的能力）

| 指标 | 数字 |
|---|---|
| 端到端意图分类准确率 | 95.8%（40 用例实测）|
| 规则分类延迟 | < 100ms |
| 平均首 token 延迟 | < 2s |
| 并发支持 | 50 用户无降级 |
| 测试覆盖率 | 25 个 pytest（单测 16 + 集成 9）|
| 退款路径覆盖 | 4 条（全覆盖）|
| Docker 服务数 | 5 个（FastAPI + MySQL + Redis + Qdrant + nginx）|
| 知识库规模 | 67 条 Qdrant 点（商品 10 + 政策 4 + FAQ 25 + 原 5）|

---

## 更新日志

| 日期 | 改动 |
|---|---|
| 2026-06-27 | 初版（V3 LangGraph 集成后）|