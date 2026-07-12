# config/prompts

业务 Prompt 集中存放目录。Sprint 2 只搭架子；Sprint 3 起把散落在业务代码中的 f-string 抽到这里。

## 命名约定

- 文件名小写 + 下划线：`intent.yaml` / `rerank.yaml` / `guard_chitchat.yaml`
- 子目录建议用两层以内，例如 `guard/chitchat.yaml` ⇒ 对应 `get_prompt_loader().load("guard/chitchat")`
- **不含路径前缀**（调用方传入相对名），**不含扩展名**

## 文件格式

每个 YAML 文件**必须**包含 `content` 字段（YAML 块字符串）：

```yaml
content: |
  你是一个电商客服 AI ...
  请严格按以下规则回答 ...
```

块字符串（`|`）保留所有换行；如需 JSON 风格字段后续可扩展 `version` / `model_params`，Sprint 3+ 按需加入。

## 调用方式

```python
from app.services.prompt_loader import get_prompt_loader

prompt_text = get_prompt_loader().load("intent")
```

## 已知约束

- name 严格白名单：仅允许小写字母 / 数字 / 下划线 / 单层 `/`；其他字符（含 `..`）一律拒绝
- 热更新：进程内 mtime 缓存，改 YAML 后下次 load 自动返回新值（**无需重启**）
- 多租户覆盖：**暂未实现**（S6 才上）

## Sprint 进度

- Sprint 2（当前）：架子 + loader，无业务 YAML
- Sprint 3 起：迁入 5 个硬编码 prompt（agent / intent / rerank / query_rewriter / guard_chitchat）
