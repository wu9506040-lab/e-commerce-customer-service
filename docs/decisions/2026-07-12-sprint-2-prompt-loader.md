# Sprint 2 启动决策 — Prompt 基础设施（2026-07-12）

> **决策记录（Sprint 开工）**：roadmap §3.3 启动条件 + 范围细化。
> **取代**：`docs/development/archive/`（V1 roadmap 已归档）
> **状态**：✅ 已完成（2026-07-12）

---

## 1. 目标

为业务 Prompt 集中化做准备：搭 `config/prompts/` 目录 + 统一加载器（loader）。

**业务驱动**：当前 5 处硬编码 Prompt（synthesizer / intent / rerank / query_rewriter / guard chitchat）导致：
- 无法版本管理（V3+ 企业定制需要）
- 无法热更新（修改 prompt 需重启）
- 无法多租户覆盖（S6 SaaS 化需要）

S2 不抽业务 Prompt（那是 S3），只搭架子。

## 2. 范围 / 不范围

| 范围 | 不范围 |
|------|--------|
| `app/services/prompt_loader.py`（Protocol + YAMLPromptLoader + 工厂） | 业务 Prompt 抽取（S3 拆 synthesizer 时做） |
| `config/prompts/.gitkeep` + `README.md` | Prompt 版本管理 / 灰度（V3+） |
| `Settings.PROMPT_DIR` 配置项 | DB 存储 Prompt |
| Dockerfile `COPY config/` 1 行 | 多租户级覆盖（S6） |
| 单元测试 21 用例 | 写并发安全（V3+） |

## 3. 文件清单

| 操作 | 路径 |
|------|------|
| 新建 | `backend/app/services/prompt_loader.py`（194 行） |
| 新建 | `backend/config/prompts/.gitkeep`（占位空目录） |
| 新建 | `backend/config/prompts/README.md`（约定文档） |
| 新建 | `backend/tests/test_prompt_loader.py`（21 用例） |
| 新建 | `docs/decisions/2026-07-12-sprint-2-prompt-loader.md`（本文件） |
| 修改 | `backend/requirements.txt`（+5 行：PyYAML==6.0.2） |
| 修改 | `backend/app/core/config.py`（+5 行：PROMPT_DIR 配置） |
| 修改 | `backend/Dockerfile`（+1 行：COPY config/） |

## 4. 关键设计决策

### 4.1 Protocol 优先（§9.3.3）
业务模块通过 `from app.services.prompt_loader import get_prompt_loader, PromptLoader`；
**禁止** `from app.services.prompt_loader import YAMLPromptLoader`（绕过依赖倒置）。

### 4.2 路径解析策略
```python
# 1. PROMPT_DIR 绝对路径 → 直接用
# 2. PROMPT_DIR 相对路径 → 相对 backend 根解析（与 cwd 无关）
backend_root = Path(__file__).resolve().parents[2]   # app/services/prompt_loader.py 三级父
return (backend_root / raw).resolve()
```
理由：本地 / 容器 / 测试 3 种 cwd 场景都能稳定定位 prompts。

### 4.3 mtime 热更新 vs 显式 reload
选择 mtime 自动检查（每次 load 都 stat）。理由：V2 阶段 prompt 数量 < 10，stat 开销 < 1ms；
运维改 YAML 后下次 load 自动生效，**比显式 API 友好**。

### 4.4 路径越权防御（双重）
- name 正则白名单：`^[a-z0-9_]+(/[a-z0-9_]+)*$`
- resolve 后前缀检查：`full_path` 必须 startswith `base_dir`

双重防御应对 name 校验绕过 + symlink 攻击。

### 4.5 threading.Lock（不引入分布式锁）
单进程读多写少场景，dict 操作 + 单锁足够；写并发留 V3+。

## 5. §4.2 跨模块例外

本次纯新增（4 个新文件）+ 3 个原文件增补（一行 +5 行 +1 行），**不跨模块**。

业务代码无须改动；S2 → S3 衔接由 Sprint 3 启动时新建 ADR。

## 6. 验证计划

| 类型 | 内容 | 通过判据 |
|------|------|----------|
| 单元 | `tests/test_prompt_loader.py` 21 用例 | 全绿 |
| 回归 | 全量 `pytest tests/` | 150 passed（129 旧 + 21 新） |
| 反向依赖 | `grep services/ imports from core/` 等 | 0 命中 |
| 文件规模 | `wc -l prompt_loader.py` | < 200 行 |
| 部署 | `docker compose config` | syntax OK（commit 4 跑过） |

## 7. commit 节奏（实际）

```
1f705fc chore(deps): 新增 PyYAML==6.0.2 锁版本
910663c feat(services): 新增 prompt_loader 统一加载器
05d5965 chore(config): 新增 PROMPT_DIR 配置 + config/prompts 架子 + Dockerfile COPY
68d5700 test(services): prompt_loader 单元测试 21 用例 + mtime 顺序 bugfix
```

每个 commit 独立可回滚；bug 在 commit 4 测试阶段第 1 次发现、第 1 次修复（无重试）。

## 8. 关闭 Roadmap V2 缺口

- **G6**（无 config/prompts 目录）→ ✅ 关闭
- **G5**（Prompt 硬编码）→ ⏸ 部分关闭：架子就位；业务抽取由 S3 完成

## 9. Sprint 3 启动前置

- ✅ Prompt loader 可用（业务模块可 import）
- ⏸ S3 需规划：5 个业务 YAML（agent / intent / rerank / query_rewriter / guard_chitchat）抽离
- ⏸ S3 需重建 synthesizer 拆分（928 → 4 模块）+ 使用 loader 加载 5 个 prompt
