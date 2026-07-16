# RAG 评测脚本说明

> 依据：B1 RAG 评测 harness 增强（commit 57eaa6a / 后续 test+docs commit）
> 适用：研发 / 评测 / CI 门禁场景

## 0. 一句话定位

3 个脚本配合使用，覆盖 RAG 检索质量全维度：

| 脚本 | 评估对象 | 核心指标 |
|---|---|---|
| `eval_hitk.py` | 检索质量 | hit@1 / hit@3 / hit@5 / hit@10 + latency |
| `eval_faithfulness.py` | 答案忠实度 | citation_rate / no_hallucination_rate / faithfulness_score |
| `compare_modes.py` | 多模式 A/B | 6 模式 hit@K + latency 对比表 |

---

## 1. 数据集说明（**local artifacts · 不参与版本控制**）

`data/eval_set_*.json` / `data/eval_faith_set.json` / `data/eval_hitk_*.json` / `data/eval_compare_report.json` **均为 local artifacts**，由 `.gitignore` 第 82 行 `data/` 整目录忽略，**不在 Git 版本控制范围内**。

| 类型 | 文件 | 用途 | 谁生成 |
|---|---|---|---|
| **评测集**（输入）| `data/eval_set_v1.json`<br>`data/eval_set_v2.json`（黄金集 30 条）| hit@K 评测输入 | `scripts/gen_eval_set.py` 自动生成 + 人工校对 |
| **评测集**（输入）| `data/eval_faith_set.json`（30 条）| 忠实度评测输入 | 手工构造 + 期望/敏感关键词 |
| **报告**（输出）| `data/eval_hitk_*.json`<br>`data/eval_faithfulness_report.json`<br>`data/eval_compare_report.json` | 评测结果存档 | 评测脚本生成 |

**部署到新环境时**：
1. 启动 Qdrant + 加载 embedding（参考 `deploy/`）
2. 跑 `scripts/gen_eval_set.py` 重新生成评估集（或从内部备份恢复）
3. **不要**把 `data/` 目录提交到 Git（避免评测集污染仓库 / 报告体积膨胀）

---

## 2. 如何跑

### 2.1 前置条件

```bash
# 启动 Qdrant + MySQL + Redis
cd deploy/
docker compose --env-file .env.dev up -d

# 加载 embedding 到 Qdrant（首次部署）
PYTHONPATH=backend python scripts/ingest_ecommerce_kb.py

# 设置环境变量（用 deploy/.env.dev 已包含）
```

### 2.2 跑 hit@K 评测

```bash
cd E:/智能客服
PYTHONPATH=backend python scripts/eval_hitk.py --input data/eval_set_v2.json

# 加 --latency-bench：每条 query 跑 3 次取中位数（防抖动）
PYTHONPATH=backend python scripts/eval_hitk.py --latency-bench

# 加 --rerank / --bm25 / --multi-query 启用各模式
PYTHONPATH=backend python scripts/eval_hitk.py --rerank --bm25
```

输出到控制台 + `data/eval_hitk_report.json`。

### 2.3 跑忠实度评测

```bash
# 准备 answers.json（query → LLM 生成答案）
PYTHONPATH=backend python scripts/gen_answers.py  # 示例脚本（自行实现）

# 跑忠实度评测
PYTHONPATH=backend python scripts/eval_faithfulness.py \
    --input data/eval_faith_set.json \
    --answers-file data/answers.json \
    --output data/eval_faithfulness_report.json

# 演示模式（不依赖外部 API，用 placeholder 答案触发 mini-judge）
PYTHONPATH=backend python scripts/eval_faithfulness.py --demo
```

### 2.4 跑 6 模式 A/B 对比

```bash
PYTHONPATH=backend python scripts/compare_modes.py \
    --input data/eval_set_v2.json \
    --output data/eval_compare_report.json

# 只跑部分模式
PYTHONPATH=backend python scripts/compare_modes.py --modes baseline rerank hybrid
```

输出对比表（按 hit@5 降序）+ 推荐建议。

---

## 3. 阈值门禁（CI 集成）

`tests/test_eval_hitk.py::test_threshold_gate_*` 定义了 CI 门禁：

| 阈值 | 含义 | 触发动作 |
|---|---|---|
| `hit@5 >= 0.6` | 检索质量达标 | pass |
| `hit@5 < 0.6` | 检索质量退化 | fail |

**集成到 CI**（参考 `.github/workflows/`）：
```yaml
- name: RAG evaluation gate
  run: |
    PYTHONPATH=backend python -m pytest tests/test_eval_hitk.py -v
```

---

## 4. 如何加新 query

### 4.1 加 hit@K 评测 query

1. 编辑 `data/eval_set_v2.json`（如不存在则从 v1 复制）
2. 加新条目：
   ```json
   {
     "query": "用户实际问法",
     "relevant_doc_id": "从 Qdrant 查到的 point UUID",
     "source": "policy_xxx / product_xxx",
     "expected_keywords": ["期望引用的关键词"]
   }
   ```
3. 跑 `PYTHONPATH=backend python scripts/eval_hitk.py` 验证

### 4.2 加忠实度评测 query

1. 编辑 `data/eval_faith_set.json`
2. 加新条目（含 expected_keywords + sensitive_keywords）：
   ```json
   {
     "query": "...",
     "expected_keywords": ["运费", "7天"],   # 答案应包含
     "sensitive_keywords": ["次日达", "免费"], # 答案不应包含（幻觉诱饵）
     "source": "policy_shipping_main"
   }
   ```
3. 跑 `eval_faithfulness.py` 验证

---

## 5. 评测指标说明

### 5.1 hit@K（检索）

`hit@K = (检索前 K 个中包含 relevant_doc_id 的比例)`

- **hit@1**：首条命中率（严格）
- **hit@3**：粗排命中率
- **hit@5**：RAG 场景常用阈值（LLM context 通常取 top-5）
- **hit@10**：宽召回上限

### 5.2 faithfulness（忠实度 · 轻量化）

`faithfulness_score = citation_rate * no_hallucination_rate`

- **citation_rate**：答案中是否引用 ≥1 个 `expected_keywords`
- **no_hallucination_rate**：答案中是否出现 `sensitive_keywords`（幻觉诱饵）
- **轻量 LLM-as-judge 兜底**：当答案 < 10 字 或 expected_keywords 抽取为空时触发；50 token 内输出 1/0 二值

| 路径 | 触发条件 | LLM 调用 | 成本 |
|---|---|---|---|
| **rule** | 答案 ≥ 10 字 且 expected_keywords 非空 | ❌ 无 | 0 |
| **mini-judge** | 答案 < 10 字 或 expected_keywords 空 | ✅ 50 token | < 1% |

**预期**：~80% 走 rule 路径，~20% 走 mini-judge 兜底。

### 5.3 latency

- **p50**：中位数延迟
- **p90**：90 分位延迟（生产环境关注）
- **max**：单条最慢（异常 case）

`--latency-bench` 模式每条 query 跑 3 次取中位数，防网络抖动。

---

## 6. 故障排查

| 现象 | 原因 | 修复 |
|---|---|---|
| `ValueError: DATABASE_URL 未设置` | 未加载 `.env.dev` | `set -a; source deploy/.env.dev; set +a` 或用 docker compose |
| `ModuleNotFoundError: No module named 'app'` | `PYTHONPATH=backend` 未设 | 加 `PYTHONPATH=backend` 前缀 |
| `Qdrant connection refused` | Qdrant 未启动 / 端口映射错 | `docker compose ps` 检查；用 `localhost:6333` 而非 `qdrant:6333` |
| 评测结果全是 0 | embedding API key 失效 | 检查 `DASHSCOPE_API_KEY` |
| `test_prompt_loader_version` 偶发失败 | known flaky（mtime reload 竞态）| 重跑即可；与本次评测无关 |

---

## 7. 版本历史

| 版本 | commit | 内容 |
|---|---|---|
| B1.1 | 57eaa6a | 新增 eval_faithfulness.py + compare_modes.py + eval_hitk.py --latency-bench flag |
| B1.2 | (TBD) | tests/test_eval_hitk.py（15 用例 mock + 阈值门禁）+ 本 README |

---

## 附录：相关文档

| 文档 | 路径 |
|---|---|
| 业务架构 | `docs/architecture/business.md` |
| 系统架构 | `docs/architecture/system.md` |
| 工程纪律 | `CLAUDE.md`（§6 验证分级 / §9.6 Prompt / §9.7 自检 5 问）|
| Roadmap | `docs/development/roadmap.md` |
| 学习日志 | `docs/learning_log.md` §38（B1 实绩）|