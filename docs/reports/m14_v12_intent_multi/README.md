# M14 V12 多意图识别 · 阶段 1 基础设施（2026-07-22）

> V12 cycle 闭环：V12-A `classify()` 新结构 + V12-B schema/灰度开关 + V12-C 4 处消费方 primary 化 + V12-D 25 个新测试。
> 两阶段策略：V12 = 多意图识别（4 类不变 · K=2）；V13 = 完整意图扩写（chitchat / complaint / K=全）。
> 公网演示入口：http://120.79.27.124:5173

---

## 1. 核心结论

| 维度 | 数值 |
|---|---|
| 部署版本 | V12-A `6f6019f` + V12-B `79a47cd` + V12-C `7dee57e` + V12-D `48ae6f9` |
| 范围收窄 | 4 类 intent 不变（refund / policy / order / product）· K=2 · 灰度默认 true |
| 验证 | 55/55 V12 相关测试 PASS · 632/635 backend 全量 PASS（3 fail = MySQL 容器环境问题） |
| 接口兼容 | 12 个测试 fixture + 6 个消费方零修改（`intent`/`confidence` 字段保留做 backward-compat 别名） |
| 双 remote | V12 4 commit 已 push Gitee origin + GitHub github |

---

## 2. V12 vs V11 行为对比

| 维度 | V11（现状）| V12（升级）|
|------|------------|------------|
| `classify()` 返回 | `{"intent": str, "confidence": float, "method": str, "entities": dict}` | `{"intents": [{"intent", "confidence"}], "primary": str, "method": str, "entities": dict}` |
| intent 数量 | 1 个 | top-K=2 个（规则天然 1 个；LLM 兜底 1-2 个）|
| primary 选择 | N/A | intents 中 confidence 最高的 |
| backward-compat | N/A | `intent` / `confidence` 别名 = primary → 12 个测试 fixture 零修改 |
| LLM 提示词 | `{"intent": "类别", "confidence": 0.0~1.0}` | `{"intents": [{"intent", "confidence"}, ...]}` （多意图 JSON 数组）|
| JSON 解析 | `re.search(r"\{[^{}]+\}", reply)`（单层）| `_find_outermost_json()` 括号配对（嵌套对象） |
| 灰度开关 | N/A | `ENABLE_MULTI_INTENT: true` （decide.yaml §10） |
| secondary 注入 | N/A | orchestrator 拼 `secondary_intent_block` 注入 `_build_chat_prompt`（context > secondary > tool 优先级）|

---

## 3. V12 commit 列表（按时间顺序 · 全部已 push 双 remote）

| Commit | 主题 | 改动文件 | 验证 |
|--------|------|----------|------|
| `6f6019f` | V12-A · `intent_service.py` 核心 | `app/services/intent_service.py` | 14/14 intent 测试 PASS · backward-compat 验证 |
| `79a47cd` | V12-B · schema + 灰度开关 | `app/schemas/intent.py` + `config/business_rules/decide.yaml` | 14/14 schema 字段断言 PASS |
| `7dee57e` | V12-C · 4 处消费方 primary 化 + secondary 注入 | `app/api/chat.py` + `app/services/chat/{orchestrator,refund_handler,prompt_assembler}.py` + `app/services/escalation_service.py` | 30/30 intent+refund+chat_policy 测试 PASS |
| `48ae6f9` | V12-D · 多意图测试 25 个 | `backend/tests/test_intent_multi.py`（新增）| 25/25 多意图测试 PASS · autouse fixture 隔离 _MULTI_INTENT_ENABLED / TOP_K |

---

## 4. V12 设计要点

### 4.1 多意图 JSON 解析（V12-A 关键 bug 修复）

**问题**：原 `_llm_classify` 用 `re.search(r"\{[^{}]+\}", reply)` 解析单意图 JSON。V12 多意图 JSON 形如 `{"intents": [{"intent": "a", "confidence": 0.9}, ...]}` 是**嵌套对象** —— 非贪婪正则会匹配到内层第一个 `}` 就停，只截到 `{"intent": "a", "confidence": 0.9}`。

**修复**：加 `_find_outermost_json(text)` 括号配对函数（处理字符串字面量避免误计数），保证匹配最外层完整 `{...}`。

```python
def _find_outermost_json(text: str) -> Optional[str]:
    """找最外层 {...}（括号配对）"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        c = text[i]
        if escape_next:        # 字符串字面量避免误计数
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None
```

### 4.2 backward-compat 别名（关键！）

```python
new_result = {
    "intents": sorted_intents,    # V12 新字段
    "primary": primary,           # V12 新字段
    "method": result["method"],
    "entities": entities,
}
# V11 别名（永远存在）→ 12 个测试 fixture + 6 个消费方零修改
new_result["intent"] = primary
new_result["confidence"] = primary_conf
```

**兼容性矩阵**：
| 调用方 | V11 写法 | V12 写法（兼容）|
|--------|----------|----------------|
| `test_synthesizer_refund.py` fixture | `{"intent": "refund_query", "confidence": 1.0, ...}` | 零修改（fixture 自己造 dict）|
| `test_intent_config.py` 5 个测试 | `result["intent"] / result["method"] / result["confidence"]` | 零修改（别名保留）|
| `test_audit_resolver.py` | `intent_result={"intent": ..., "entities": ...}` | 零修改（fixture 自己造 dict）|
| `chat.py:314` | `pre_intent.get("intent")` | 改为 `pre_intent.get("primary", pre_intent.get("intent"))` 双轨 |
| `orchestrator.py:166` | `intent = intent_result["intent"]` | 改为 `primary = intent_result["primary"]` |
| `refund_handler.py:65,84` | `intent_result.get("intent", ...)` | 改为 `intent_result.get("primary", intent_result.get("intent"))` 双轨 |
| `escalation_service.py:305` | `intent_result.get("intent")` | 改为 `intent_result.get("primary") or intent_result.get("intent")` |

### 4.3 secondary 注入 prompt（V12-C 关键新功能）

orchestrator 在分派前构造 `secondary_intent_block`：

```python
all_intents = intent_result.get("intents", [])
secondary_intents = [i for i in all_intents if i["intent"] != primary]
secondary_intent_block = ""
if secondary_intents:
    secondary_lines = [f"- {i['intent']}（置信度 {i['confidence']:.2f}）" for i in secondary_intents]
    secondary_intent_block = (
        "用户问题可能还涉及以下意图，请在回答 primary 意图时一并简要覆盖：\n"
        + "\n".join(secondary_lines)
    )
```

`_build_chat_prompt` 新参数 `secondary_intent_block` 在 context_block 之后、tool_block 之前：

```
【当前场景】(M9.5 用户跳转 context)         ← M9.5 最高优先级
{context_block}

【用户可能的次要问题】(V12 多意图识别)    ← V12 次高
{secondary_intent_block}

【事实陈述】(最高优先级)                  ← 工具结果
{tool_block}
...
```

### 4.4 灰度开关

```yaml
# decide.yaml §10
# === 10. 多意图识别灰度开关（V12 · 阶段 1 多意图识别 / 阶段 2 V13 完整意图扩写）===
ENABLE_MULTI_INTENT: true
```

`false` → 退回 V11 单意图行为（`_llm_classify` 旧 prompt，intents 长度仍为 1）。

---

## 5. V12 7 个 TestClass 覆盖矩阵

| TestClass | 测试数 | 覆盖点 |
|-----------|--------|--------|
| TestClassifyV12NewStructure | 7 | intents[] 列表 / primary 字段 / 降序 / intent 别名 / confidence 别名 / method 保留 / entities 保留 |
| TestRuleClassifySingleIntent | 4 | 4 类规则命中各 1 个：refund / policy / product / order |
| TestMultiIntentGraySwitch | 2 | ENABLE_MULTI_INTENT=true 走 multi 路径；=false 走 single 路径（intents 仍 1 个）|
| TestLLMMultiIntentParse | 5 | 多意图 JSON 成功 / 单意图 JSON / ```json``` fence 容错 / 非法 JSON fallback default / 非法 intent 过滤 |
| TestTopKTruncation | 2 | `_llm_classify_multi` TOP_K 截断 + `_wrap_with_intent_alias` 二次截断 |
| TestSecondaryIntentBlockInjection | 3 | secondary 出现在 prompt / 空时不出现 / 优先级 context > secondary > tool |
| TestIntentResponseSchema | 2 | schema 接受新结构 / 序列化 intent 字段 = primary |

**autouse fixture 隔离**：
```python
@pytest.fixture(autouse=True)
def _isolate_intent_state_after():
    saved_enabled = intent_service._MULTI_INTENT_ENABLED
    saved_top_k = intent_service.TOP_K
    yield
    intent_service._MULTI_INTENT_ENABLED = saved_enabled
    intent_service.TOP_K = saved_top_k
    reset_config_loader()
```

---

## 6. V12 已知限制

| # | 限制 | 原因 | V13 修复 |
|---|------|------|----------|
| 1 | handle_refund_v3 接收 secondary_intent_block 但**不传 RefundFlow** | RefundFlow 是 M14 V3 重构核心（5 commit），V12 不动 RefundFlow 避免改膨胀 | V13 阶段 2 一起处理 RefundFlow 4 节点注入 |
| 2 | K=2（限 1 个 secondary）| V12 阶段 1 验证基础设施；K=全可能让 LLM prompt 膨胀 | V13 K=全 |
| 3 | 4 类 intent 不变（无 chitchat / complaint）| V12 阶段 1 验证多意图框架 | V13 扩完整意图种类 |
| 4 | top-K 截断无 per-intent 阈值 | V12 阶段 1 简化 | V13 引入 confidence 阈值（< 0.5 截掉）|

---

## 7. §9 架构约束自检

| # | 约束 | 满足方式 |
|---|------|----------|
| §5 Scope Lock | 跨 4 模块（intent_service + schema + 配置 + 5 消费方）| §4.2 4 要素已列（业务原因 / 接口变化 / 影响范围 / 隔离策略）|
| §9.3 接口契约 | `classify()` / `_build_chat_prompt` / 各 handler 签名扩展（向后兼容）| ✅ 加新字段 / 加新参数（带默认值）|
| §9.4.2 配置分离 | `ENABLE_MULTI_INTENT` 灰度开关放 decide.yaml §10 | ✅ 与 HALLUCINATION_REPLACE_FAKE_STATUS 风格一致 |
| §9.5.1 5 防 · 防幻觉 | LLM 多意图 prompt 严格 JSON；非法 fallback 到 default | ✅ |
| §9.6 Prompt 独立管理 | 多意图 LLM prompt 写在 intent_service.py（待 V13 抽到 config/prompts/）| ⚠️ V12 暂不抽（与 V11 _llm_classify 一致风格）|
| §9.7 自检 5 问 | 不引入跨模块耦合；orchestrator 仍是单一编排者 | ✅ |
| §9.8 8 件套 | 非新模块（仅扩展现有 intent_service + schema）| ✅ N/A |

---

## 8. V12 上线观察清单（V13 启动前置）

| 观察项 | 周期 | 验证目标 |
|--------|------|----------|
| secondary 注入后 LLM 答全率 | 1-2 周 | 多意图 query 答全率从单意图时 ~70% 提升到 ≥ 90% |
| 用户自助率 | 2-4 周 | 多意图 query 不再触发 escalate |
| classify 性能 | 1 周 | LLM 兜底路径 +50ms 内（多意图 JSON 解析开销）|
| LLM JSON 解析失败率 | 1 周 | < 1%（括号配对解析 + 非法 intent 过滤双保险）|
| 用户体感"AI 答得更全" | 2-4 周 | 抽样 50 条多意图 query 看用户追答率下降 |

**V13 启动条件**：上述 4 项稳定 + 灰度关闭 fallback 验证通过。

---

## 9. V13 衔接（阶段 2 完整意图扩写）

| 维度 | V12 现状 | V13 计划 |
|------|----------|----------|
| 意图枚举 | 4 类 | 7+ 类（+ chitchat / complaint / 视情况加 logistics）|
| K | 2 | 全（K=N）+ per-intent confidence 阈值（< 0.5 截掉）|
| chitchat 处理 | N/A | 短答模板（不调 RAG / LLM）|
| complaint 处理 | N/A | 接 P0 escalate（已有 escalation_service）|
| 情绪识别联动 | N/A | complaint + 情绪分数 → 优先 escalate |
| 评测口径 | "5 真指标" + 政策覆盖率 | + 「多意图召回率」= top-K 命中率 / 「chitchat 短路率」 |

**V13 不动 V12 基础设施**：只改 INTENT_RULES + TOP_K + 加 chitchat/complaint handler，classify() / orchestrator / prompt_assembler 零修改。
