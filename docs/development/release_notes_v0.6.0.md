# v0.6.0 Release Notes

> 发布日期：2026-07-14
> 基于：`f853c4d`（master HEAD）
> 前序 Tag：`sprint-4-complete`（`b3d7f47`，Sprint 4 主线收尾）

---

## 概览

本次发布在 Sprint 4 业务规则 YAML 化全量闭环之上，新增两条业务能力纵深：

- **Phase 4 A4 多 Query 检索增强**：检索侧由"单查询 → RAG"升级为"多路查询改写 + RRF 融合"，降低改写/上下文相关 query 的召回漂移。
- **P2 长程记忆**：跨 session 用户画像（偏好 / 历史 SKU / 累计对话 / LLM 摘要）写入 prompt，提升老用户的连续咨询体验。

两条新能力均以**灰度开关**形式上线，默认关闭，便于按租户 / 按流量比例分批开启。

---

## 新功能

### 1. Phase 4 A4 — 多 Query 检索增强

| 项 | 说明 |
|----|------|
| 入口 | `ENABLE_MULTI_QUERY`（默认 `false`） |
| 机制 | query_rewriter 多路改写（3 路：原查询 / 同义改写 / 上下文补全）+ policy 多路 RRF 融合 |
| 评估 | `eval_hitk --multi-query` 脚本（与既有 eval_hitk 同接口） |
| 失败回退 | 任一路失败 → 单路兜底，不影响主链路 |
| 改动范围 | `query_rewriter.py` / `policy.py` / `settings.py`（仅增量） |

### 2. P2 长程记忆 — 跨 session 用户画像

| 项 | 说明 |
|----|------|
| 入口 | `ENABLE_USER_PROFILE`（默认 `false`） |
| 数据 | `user_profiles` 表（PK: `user_id`，1:1 → `users.id`） |
| 字段 | `summary` / `frequent_skus` / `preferences` / `interaction_count` / `last_active_at` / `deleted` |
| 接口 | `profile_service` 5 写路径 + `to_prompt_block()` 格式化 |
| Prompt 注入 | `prompt_assembler._build_context_block(profile_block=...)` |
| 隐私保护 | `user_id=0` 短路 / `clear()` 软删 / 反幻觉 hard label |
| 改动范围 | 新增 `models/user_profile.py` + `services/profile_service.py` + `02_user_profiles.sql`；orchestrator / chat.py 增量集成 |

---

## 修复

| Commit | 内容 |
|--------|------|
| `93c94c6` | `to_prompt_block` 整体硬截断（prefix 算入 `max_len`，避免 prompt 膨胀） |

---

## 测试

| 维度 | 数量 | 说明 |
|------|------|------|
| 全套 pytest | **270 PASS** | 含 19（Phase 4 A4）+ 27（P2 长程记忆）新增用例 |
| Phase 4 A4 | 19 | `test_query_rewriter` + `test_policy_rrf` |
| P2 长程记忆 | 27 | `test_profile_service`（8 类：get_or_create / update_summary / append_frequent_skus / increment_interaction / clear / to_prompt_block / 隐私边界 / 灰度开关） |
| CI | success | GitHub Actions run #29336234078 |

---

## 文档

| 文件 | 内容 |
|------|------|
| `docs/learning_log.md` §27-§32 | Sprint 4 收尾 + Phase 4 A4 + P2 长程记忆 复盘 |
| `docs/development/roadmap.md` §3.5.1 / §3.8 / §3.9 | 演进基线新增条目 |
| `docs/development/current_status.md` v7 | 会话级状态同步 |

---

## 升级与回滚

| 操作 | 步骤 |
|------|------|
| 启用多 query | `ENABLE_MULTI_QUERY=true`（任一租户） |
| 启用长程记忆 | (1) `deploy/mysql/init/02_user_profiles.sql` 初始化表 (2) `ENABLE_USER_PROFILE=true` |
| 回滚 | 两个开关均默认关闭 → 不配置 = 不启用，新功能不触发 |
| 数据迁移 | `user_profiles` 新表，无需迁移；其余表零变更 |

---

## 已知限制

| 项 | 影响 | 后续 |
|----|------|------|
| 多 query 改写仅 3 路 | 召回覆盖有限 | Phase 4 A5+ 加并行多路 / RRF 加权 / HyDE |
| 长程记忆 summary 由业务层生成（暂未接 LLM 自动摘要） | 需要运营侧补 LLM 摘要步骤 | Sprint 5 Prompt 版本管理 + LLM 摘要 hook |
| profile 跨端不同步 | 用户切换设备看不到 | 多端合并 = 数据中台（G11-G13 范围） |