# config/prompts

业务 Prompt 集中存放目录。Sprint 2 搭架子；Sprint 3 起把散落在业务代码中的 f-string 抽到这里；Sprint 5 起支持多版本 manifest。

## 命名约定

- 文件名小写 + 下划线：`intent.yaml` / `rerank.yaml` / `guard_chitchat.yaml`
- 子目录建议用两层以内，例如 `guard/chitchat.yaml` ⇒ 对应 `get_prompt_loader().load("guard/chitchat")`
- **不含路径前缀**（调用方传入相对名），**不含扩展名**

## 文件格式（两种模式并存）

### 模式 A：兼容模式（旧 YAML，最简单）

```yaml
content: |
  你是一个电商客服 AI ...
  请严格按以下规则回答 ...
```

适用：单版本 prompt，无需多版本管理。

### 模式 B：manifest 模式（多版本，Sprint 5 起）

```yaml
default_version: v1
versions:
  v1:
    file: agent_v1.yaml   # 引用外部文件
    stable: true
    note: 稳定版说明
  v2:
    content: |             # 或内联 content（与 file 二选一，content 优先）
      直接写 v2 内容
    stable: false
    note: 实验版说明
```

每个 version 必须有 `file` 或 `content` 之一（content 优先）。

适用：需要 A/B / 实验 / 回滚的 prompt。

## 调用方式

```python
from app.services.prompt_loader import get_prompt_loader

# 兼容模式 / manifest 模式都支持
prompt_text = get_prompt_loader().load("agent")              # 走 default_version 或单版本
prompt_text = get_prompt_loader().load("agent", version="v2") # 显式指定版本
```

## 多版本管理（manifest 模式）

| 场景 | 调用 | 行为 |
|------|------|------|
| 加载默认版本 | `load("agent")` | 走 manifest 的 `default_version` |
| 加载指定版本 | `load("agent", version="v2")` | 加载 v2（即使 default=v1） |
| 回滚 | 改 manifest `default_version` | 下次 load 拿新版本，无需重启 |
| 加新版本 | 在 manifest 加 `v3` + 新文件 | 调用方显式 `version="v3"` 才生效 |

### 灰度（traffic_ratio）

**Sprint 5 阶段 1 暂未实现**。后续阶段会在 manifest 里加 `traffic_ratio` 字段 + `hash_key` 灰度。

## 已知约束

- name 严格白名单：仅允许小写字母 / 数字 / 下划线 / 单层 `/`；其他字符（含 `..`）一律拒绝
- 热更新：进程内 mtime 缓存（按 `(name, version)` 区分），改 YAML 后下次 load 自动返回新值（**无需重启**）
- 多租户覆盖：**暂未实现**（S6 才上）
- 灰度比例：**暂未实现**（Sprint 5 后续阶段）
- DB 存储：**暂未实现**（V3+ 评估）

## Sprint 进度

- Sprint 2：架子 + loader，无业务 YAML
- Sprint 3 起：迁入 5 个硬编码 prompt（agent / intent / rerank / query_rewriter / guard_chitchat）
- Sprint 4：业务规则 YAML 化（与 prompt 分离到 `config/business_rules/`）
- Sprint 5 阶段 1：manifest 多版本机制 + 兼容模式（agent.yaml 改示范）
- Sprint 5 后续：灰度 + 其他 YAML 按需迁移
