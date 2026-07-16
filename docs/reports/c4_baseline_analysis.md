# C4 Baseline Analysis（2026-07-16 · P0 修复后验证）

> 文档代号：C4A-V1
> 任务来源：SOP-V1 §1 L2 任务（验证 P0 修复效果，不开发）
> 验证方式：只读检查 + 不执行 eval（环境不具备执行条件，见下）
> 关联 commit：`022453c` (P0-1/2/3 fix) + `432e3bb` (P0-4 test+models)

---

## 0. 执行结论摘要（先看这里）

| 维度 | 结论 |
|---|---|
| P0 修复是否进入 ECS 容器？ | **❌ 否**（详见 §1） |
| eval_agent_fc.py `--mode live` 是否可用？ | **❌ 否**（3 auth bug 未修，详见 §2） |
| ENABLE_AGENT_FC 灰度是否已开？ | **❌ 否**（灰度保持关，详见 §3） |
| 本次能否产出新 baseline？ | **❌ 不能**（双重 blocker） |
| 上次 baseline 数据 | tool_sel=0.133 / kw_match=0.000 / halluc=1.000，详见 §4 |
| 建议下一步 | 见 §6 |

---

## 1. 环境诊断（只读检查）

### 1.1 本地代码状态

```text
git status: clean
master:    432e3bb test(models): ORM SQLite 兼容 + 真 DB 集成测试样板
           022453c fix(tools+rag): C4 blocker 修复 + refund tool + Embedding降级
created:   2026-07-16 22:06:19+0800（CST）
```

P0-1/2/3 修复在本地代码已落地：
- `tools/registry.py` 含 `OrderTool.get_order_by_no` + `check_refundable` ToolSpec（8 处匹配）
- `services/rag/pipeline.py` embed/search 加 try/except 降级

### 1.2 ECS 容器状态（关键阻塞）

| 检查项 | 结果 |
|---|---|
| API 健康 | ✅ `200 OK` (`http://120.79.27.124:8000/health`) |
| OpenAPI | ✅ `/openapi.json` 200 OK，含 `/api/chat` + `/api/chat/resume` |
| 容器 `customer-service-api` 创建时间 | **2026-07-16 21:12:56**（早于本地 P0 commit 53 分钟） |
| 容器内 `tools/registry.py` 含 `get_order_by_no` | **❌ 0 命中**（部署的是修复前代码） |
| 容器内 `tools/registry.py` 含 `check_refundable` | **❌ 0 命中** |
| 容器内 `services/rag/pipeline.py` 含降级 try/except | **❌ 0 命中** |

**核心结论**：ECS 容器跑的是 **修复前** 代码。即使重新跑 `--mode live`，验证的不是 P0 修复效果，而是旧代码的行为。

### 1.3 ENABLE_AGENT_FC 灰度状态

| 检查项 | 结果 |
|---|---|
| 容器环境变量 `ENABLE_AGENT_FC` | **❌ 未设置**（grep 0 命中） |
| 容器环境变量 `MAX_AGENT_TURNS` | **❌ 未设置** |
| .env.dev 中对应行 | **已回滚删除**（C4 共识："灰度保持关"） |

**核心结论**：即便 P0 修复进入容器，FC 灰度开关仍为关闭 → `agent_runner.py:138-142` 抛 `RuntimeError` → 走 V1.2 fallback，无法验证 FC 路径。

### 1.4 单次 docker container 信息

```text
NAMES:                customer-service-api
IMAGE:                customer-service-api:dev
CREATED AT:           2026-07-16 21:12:56 +0800 CST
STATUS:               Up 57 minutes (healthy)

（其他容器均为 2026-07-04 启动，已运行 12 天）
customer-service-frontend  nginx:alpine
customer-service-redis      redis:7-alpine
customer-service-qdrant     qdrant/qdrant:v1.10.1
customer-service-mysql      mysql:8.0
```

镜像 `customer-service-api:dev` 是 21:12:56 构建的，**早于本地 P0 commit（22:06:19）53 分钟**。说明容器是用 21:12 时点的旧代码构建的，P0 修复未进入。

---

## 2. eval_agent_fc.py `--mode live` 阻断（已知 3 auth bug）

`scripts/eval_agent_fc.py --mode live` 当前**不可直接执行**，已知 3 处 bug（详见 `feedback_eval_agent_fc_auth_bugs.md`）：

| # | bug | 现状 | 修复 |
|---|---|---|---|
| 1 | 登录 Content-Type | `application/json` + `json.dumps` | 应为 `application/x-www-form-urlencoded` + `urllib.parse.urlencode` |
| 2 | 鉴权机制 | `Authorization: Bearer {token}` | 服务端用 httpOnly Cookie；登录响应无 token；应用 `HTTPCookieProcessor` + cookie jar |
| 3 | SSE 端点路径 | `/api/chat/stream` | 实际 `/api/chat`（SSE 通过 OpenAPI 不显式列在 paths 里） |

按本任务约束"禁止修改业务代码 / 禁止修改 eval 脚本"，**这 3 处 bug 不在本次修复范围**。

C4 第一次跑 live 时是绕过这 3 bug 写的临时 driver (`scripts/eval_agent_fc_live.py`，已删)，产出基线 `data/eval_agent_fc_live_baseline.json`。

---

## 3. 不执行 `eval_agent_fc.py --mode live` 的原因

按 SOP-V1 §1 L2 任务的"前瞻 8 类逆向审查"权衡：

| # | 异常类型 | 当前是否会触发 |
|---|---|---|
| 1 | 参数错误 | N/A |
| 2 | 数据不存在 | N/A |
| 3 | 权限失败 | eval auth code 会 422（bug #2）→ exit 1 |
| 4 | 第三方服务失败 | **DLLM API 异常**（即使通过 login，FC 路径不启用） |
| 5 | LLM 异常 | 即使启用，容器代码仍为旧版本 |
| 6 | 超时 | N/A |
| 7 | 重复调用 | N/A |
| 8 | 回滚 | N/A |

**实际触发**：即使修好 3 个 auth bug，eval 仍走 V1.2 路径（FC 未开）→ **baseline 数据含义 = "P0 修复前 RAG 行为"，不是 FC 真实质量**。无验证价值。

按 SOP-V1 §1 L2 "判断不出默认 +1 级"原则（环境问题保守做法），叠加"任务约束=验证不开发"，本报告**不执行 eval**，只报告现状。

---

## 4. 历史 baseline 数据（C4 第一次跑）

来源：`data/eval_agent_fc_live_baseline.json`（local artifact，`data/` 已 .gitignore）

```text
样本量:        30 条（5 类 order/product/policy/mixed/direct）
tool_selection_accuracy:        0.133   （仅 direct 4 条命中"不调工具"）
answer_keyword_match:            0.000   （fallback 文本不含评测关键词）
hallucination_free_rate:         1.000   （fallback 文本相对保守）
latency p50/p90:                 3806/5904 ms
```

### 4.1 历史失败案例分类（按本次任务模板 5 类）

| 分类 | 数量 | 主要表现 | 根因 |
|---|---|---|---|
| **A. Tool 选择错误** | ~6 条（混合类 + 复杂订单类） | LLM 调对工具返回 error 后放弃 | ✅ P0-1 已修（`get_order_detail → get_order_by_no`）|
| **B. Tool 参数错误** | ~3 条 | LLM 提取 order_no / keyword 不准确 | 需 LLM 行为调优，不在 P0 范围 |
| **C. Tool 执行错误** | ~10 条 | `AttributeError: get_order_detail` 被 dispatch 吞 | ✅ P0-1 已修 |
| **D. LLM 规划错误** | ~5 条 | LLM 调错工具类型（如 refund 场景调 lookup_order） | 部分 ✅ P0-2 已注册 check_refundable，可缓解 |
| **E. 环境问题** | ~6 条（容器无 FC 代码） | server 实际跑旧代码，FC 不进路径 | ❌ 未修，需镜像重建 |

---

## 5. 是否达到 ≥0.7 灰度启用条件

| 指标 | 当前值 | 阈值 | 结论 |
|---|---|---|---|
| tool_selection_accuracy | 0.133 | ≥ 0.7 | ❌ 未达 |
| **新 baseline（修复后）** | **无法产出** | — | **不能上线 ENABLE_AGENT_FC** |

**核心结论**：**不达到灰度开启条件。灰度继续保持 False。**

---

## 6. 建议下一步（按决策大小排序）

### 6.1 P0：构建并部署包含 P0-1/2/3 的 ECS 镜像

| 步骤 | 风险 | 工作量 |
|---|---|---|
| 1. 本地 `docker buildx build --platform linux/amd64 -t customer-service-api:dev-p0 .` | 低（重建） | 5 分钟 |
| 2. `docker save` + scp 到 ECS + `docker load` | 低（已有 SOP） | 5 分钟 |
| 3. `docker compose -p customer-service --env-file .env.dev up -d --no-deps api` | 中（服务短暂中断） | 2 分钟 |

**前置**：你需要授权 ECS 服务短暂重启（health check 失败 → image rebuild 不影响 API）

### 6.2 P0：在 ECS 注入 ENABLE_AGENT_FC=true

需同时：
- `.env.dev` 加回 `ENABLE_AGENT_FC=true` + `MAX_AGENT_TURNS=5`
- `docker-compose.yml` `environment:` 块**显式列入**这两个变量（详见 `feedback_docker_compose_env_vs_envfile.md`，仅 .env.dev 加不等于进容器）
- 重启 API 容器

### 6.3 P1：修 eval_agent_fc.py 3 auth bug（独立 commit）

按 `feedback_eval_agent_fc_auth_bugs.md` 给的修复模板，可在 1 个 fix commit 内完成（~30 行）。修完后 `--mode live` 即可独立产出 baseline，无需绕道临时 driver。

### 6.4 P1：CI/CD 防镜像陈旧

按 C4 共识：手动 `docker buildx` 镜像陈旧是反复出现的根因。建议加 GitHub Actions：
- push → trigger `docker buildx build` → publish image tag → ECS pull
- 避免"本地 commit → ECS 镜像"时间差

### 6.5 决策矩阵（推荐顺序）

| 选项 | 范围 | 工作量 | 价值 |
|---|---|---|---|
| **A. 最小验证路径** | 6.1 + 6.2 + 6.3 一起做（镜像重建 + 启灰度 + 修 eval 脚本） | 半天 | 一次性产出真正验证 P0 修复效果的新 baseline |
| B. 不动 ECS，仅 6.3（修 eval 脚本） | 仅改本地 eval 脚本 | 1 小时 | 仍是旧 baseline，无验证 P0 修复的价值 |
| C. 全做（6.1-6.4） | 完整闭环 | 1-2 天 | 一次性解决 C4 + eval + CI 三个老问题 |

**推荐 A**：最小投入获得最大验证价值。

---

## 7. 验证手段受限声明

本次验证：
- ✅ 本地代码确认 P0 修复到位
- ✅ ECS 服务可达性确认
- ❌ ECS 容器代码版本确认（旧）
- ❌ ENABLE_AGENT_FC 环境变量确认（未设）
- ❌ eval_agent_fc.py --mode live 实际跑通（3 auth bug）
- ❌ 新 baseline 数字产出

按 SOP-V1 §1 L2 任务"宁严勿松"原则，**遇到环境约束时**默认不强行执行，而是先报告状态再让你决策。

---

> 维护：本报告是 SOP-V1 §1 L2 任务首个落地产物；后续每次 C4 重跑都应产出一份 baseline_analysis.md 追加到本目录。
