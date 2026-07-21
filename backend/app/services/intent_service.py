"""
Intent Service（M3 新增 · V12 多意图识别升级）

按 PROJECT_DESIGN.md §3 + §7：
- 4 类意图：order_query / refund_query / product_query / policy_query
- 三级 fallback：规则（关键词+正则）→ LLM 兜底 → 默认 policy_query
- 同时抽取实体（order_no / sku）

V12 升级（2026-07-22）：
- 多意图识别（灰度 ENABLE_MULTI_INTENT 开关，默认 true）
- classify() 返新结构：intents[] + primary + 保留 intent/confidence 别名（向后兼容）
- 规则层天然单意图（intents=[1 个]）；LLM 兜底层可识别多意图（top-K=2）
- secondary intents 由 orchestrator 拼到 prompt context
- 12 个测试 fixture + 6 个消费方零修改（"intent"/"confidence" 字段仍存在）
- V13 计划：扩 chitchat / complaint / 等 + K=全（不动 V12 基础设施）
"""
import json
import logging
import re
from typing import Optional

from app.core.providers.llm import get_llm_provider
from app.schemas.intent import IntentEntities, IntentType
from app.services.config_loader import get_config_loader

logger = logging.getLogger(__name__)


# =============================================================
# 业务规则（启动期加载一次，来自 config/business_rules/intent.yaml）
# 改阈值/规则 → 改 YAML → 重启服务（roadmap §3.5 不参与热更新）
# 单一真相源：intent_service.py 是唯一消费者
# 注：app/services/chat/prompt_assembler.py 有 _ORDER_NO_RE 独立副本（M13 同步过），
#     本次不合并（跨模块 + YAGNI），记录到 learning_log §29 已知限制
# =============================================================
_RULES = get_config_loader().load("intent")

# IntentType 是 Literal["order_query", "refund_query", "product_query", "policy_query"]
# YAML key 在运行时就是 str（Literal 的运行时类型就是 str），但加载时必须校验合法
_VALID_INTENTS = frozenset(IntentType.__args__)

# V12：多意图识别灰度开关
#   - true（默认 → V12 行为）：classify() 返 {"intents": [...], "primary": str, ...}
#   - false（V11 行为）：classify() 返相同结构，但 intents 仅含 1 个（与 V11 行为一致）
# 关闭时仍保留 "intent"/"confidence" 别名（=primary），12 个测试 fixture 零修改
try:
    _RULES_DECIDE = get_config_loader().load("decide")
    _MULTI_INTENT_ENABLED: bool = bool(_RULES_DECIDE.get("ENABLE_MULTI_INTENT", True))
except Exception:
    _MULTI_INTENT_ENABLED = True  # 默认开（与 V11 系列默认 true 对齐）

# V12：多意图 top-K 截断（避免 secondary 注入膨胀 prompt）
#   - 规则命中：天然 1 个
#   - LLM 命中：保留 top-K=2
#   - 默认 fallback：1 个 policy_query
# V13 扩 K=全时改这里即可，不动 V12 基础设施
TOP_K: int = 2


def _pick_primary(intents: list[dict]) -> tuple[str, float]:
    """从多意图列表选 primary（按 confidence 降序第一个）。

    V12 范围：规则层 + 默认 fallback 路径 intents 只含 1 个；LLM 路径可能含 1-2 个。
    V13 扩 K=全时仍走相同逻辑（排序后取首）。

    Returns:
        (primary_intent, primary_confidence)
    """
    if not intents:
        return "policy_query", 0.5  # 兜底兜底
    sorted_intents = sorted(intents, key=lambda x: x.get("confidence", 0.0), reverse=True)
    return sorted_intents[0]["intent"], sorted_intents[0]["confidence"]


def _find_outermost_json(text: str) -> Optional[str]:
    """找最外层 {...}（括号配对，V12 多意图 JSON 提取专用）。

    与 re.search(r"{[\\s\\S]*?}", text) 不同：后者非贪婪会匹配到内层第一个 }
    就停（多意图 JSON 嵌套时只截到第一个子对象）。本函数用计数器配对，
    保证匹配最外层完整 {...}。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        c = text[i]
        # 简单处理字符串字面量（避免 "}" 在字符串内被计数）
        if escape_next:
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
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _wrap_with_intent_alias(result: dict, query: str) -> dict:
    """V12：classify() 返新结构 + 保留 intent/confidence 别名。

    新结构（V12 唯一对外契约）：
        {
            "intents": [{"intent": str, "confidence": float}, ...],  # top-K 排序后
            "primary": str,                                          # 最高 confidence 的 intent
            "method": "rule" | "llm" | "default",
            "entities": {"order_no": str|None, "sku": str|None, "keywords": list[str]},
        }

    向后兼容（关键！）：
        - 旧 "intent" 字段保留 = primary（12 个测试 fixture + 6 个消费方零修改）
        - 旧 "confidence" 字段保留 = primary 的 confidence
    """
    intents = result.get("intents") or []
    entities = result.get("entities") or IntentService._extract_entities(query)

    # 防御性补救：LLM 失败后 fallback 没填 intents → 从旧结构补救
    if not intents:
        if "intent" in result:
            intents = [{"intent": result["intent"], "confidence": result.get("confidence", 0.5)}]
        else:
            intents = [{"intent": "policy_query", "confidence": 0.5}]

    # top-K 截断 + 降序（防御性二次截断，正常 classify() 内已截断）
    sorted_intents = sorted(intents, key=lambda x: x.get("confidence", 0.0), reverse=True)[:TOP_K]

    primary, primary_conf = _pick_primary(sorted_intents)

    # 构造新结构
    new_result = {
        "intents": sorted_intents,
        "primary": primary,
        "method": result.get("method", "default"),
        "entities": entities,
    }
    # 兼容旧字段（V11 行为：直接读 .intent / .confidence 仍可用）
    new_result["intent"] = primary
    new_result["confidence"] = primary_conf
    return new_result


# 意图分类规则（顺序敏感；dict 保序：Python 3.7+ dict 迭代顺序 = 插入顺序）
# 顺序：refund_query → policy_query → order_query → product_query（与原 INTENT_RULES 一致）
INTENT_RULES: dict[str, list[str]] = {}
for _intent, _patterns in _RULES["INTENT_RULES"].items():
    if _intent not in _VALID_INTENTS:
        raise ValueError(
            f"intent.yaml 含非法 intent: {_intent!r}（合法值: {sorted(_VALID_INTENTS)}）"
        )
    INTENT_RULES[_intent] = list(_patterns)  # 复制防止外部 mutate

# 实体抽取正则（启动期编译一次；运行时 re.search 直接复用）
# YAML 的 ORDER_NO_RE_FLAGS / SKU_RE_FLAGS 是字符串（如 "IGNORECASE"），
# getattr(re, name) 查找 re 模块对应属性；非法名 → AttributeError（启动期 fail-fast）
ORDER_NO_RE = re.compile(
    _RULES["ORDER_NO_RE_PATTERN"],
    getattr(re, _RULES["ORDER_NO_RE_FLAGS"]),
)
SKU_RE = re.compile(
    _RULES["SKU_RE_PATTERN"],
    getattr(re, _RULES["SKU_RE_FLAGS"]),
)


class IntentService:
    """意图分类服务（规则优先 + LLM 兜底 · V12 多意图识别升级）"""

    @staticmethod
    def classify(query: str, last_intent: Optional[IntentType] = None) -> dict:
        """
        分类入口（V12 多意图升级）

        Args:
            query: 用户问题
            last_intent: 上一轮意图（V2.6 启用；当前阶段透传不影响分类）

        Returns:
            V12 新结构（intents[] + primary）：
                {
                    "intents": [{"intent": str, "confidence": float}, ...],  # top-K 排序后
                    "primary": str,                                          # 最高 confidence 的 intent
                    "method": "rule" | "llm" | "default",
                    "entities": {"order_no": str|None, "sku": str|None, "keywords": list[str]},
                    # 向后兼容别名（V11 行为不变 → 测试 fixture 零修改）：
                    "intent": str,         # = primary
                    "confidence": float,   # = primary 的 confidence
                }
            ENABLE_MULTI_INTENT=false 时仍返上述结构（intents 长度为 1）；"intent" 别名永远存在。
        """
        entities = IntentService._extract_entities(query)

        # 1. 规则匹配（天然单意图 → intents 长度为 1）
        rule_result = IntentService._rule_classify(query)
        if rule_result:
            rule_result["entities"] = entities
            logger.info(f"intent(rule): {rule_result['primary']} query={query[:30]}")
            return _wrap_with_intent_alias(rule_result, query)

        # 2. LLM 兜底（V12：根据灰度开关走单/多意图路径）
        try:
            if _MULTI_INTENT_ENABLED:
                llm_result = IntentService._llm_classify_multi(query)
            else:
                llm_result = IntentService._llm_classify(query)
            llm_result["entities"] = entities
            logger.info(
                f"intent(llm): intents={[i['intent'] for i in llm_result.get('intents', [])]} "
                f"primary={llm_result.get('primary')} query={query[:30]}"
            )
            return _wrap_with_intent_alias(llm_result, query)
        except Exception as e:
            logger.warning(f"intent llm fallback 失败: {e}")

        # 3. 默认 policy_query（兜底兜底）
        return _wrap_with_intent_alias({
            "intents": [{"intent": "policy_query", "confidence": 0.5}],
            "primary": "policy_query",
            "method": "default",
            "entities": entities,
        }, query)

    # ---------- 私有 ----------

    @staticmethod
    def _rule_classify(query: str) -> Optional[dict]:
        """规则匹配：命中即返回（V12：单意图包装成 intents[] 列表，top-K=1）"""
        for intent, patterns in INTENT_RULES.items():  # dict 保序 → 与原 tuple 列表顺序一致
            for pattern in patterns:
                if re.search(pattern, query, re.IGNORECASE):
                    return {
                        "intents": [{"intent": intent, "confidence": 1.0}],
                        "primary": intent,
                        "method": "rule",
                    }
        return None

    @staticmethod
    def _extract_entities(query: str) -> dict:
        """抽取订单号 / SKU"""
        order_no_match = ORDER_NO_RE.search(query)
        sku_match = SKU_RE.search(query)
        return {
            "order_no": order_no_match.group(0).upper() if order_no_match else None,
            "sku": sku_match.group(0).upper() if sku_match else None,
            "keywords": [],
        }

    @staticmethod
    def _llm_classify(query: str) -> dict:
        """LLM 兜底分类（V11 旧行为：单意图；V12 默认走 _llm_classify_multi）"""
        # 注意：让 LLM 输出严格 JSON，避免解析失败
        prompt = f"""你是电商客服意图分类器。判断用户问题属于以下 4 类之一：
- order_query: 订单状态、物流、快递、发货、签收
- refund_query: 退款、退货、换货、不想要了
- product_query: 商品参数、价格、库存、SKU、推荐
- policy_query: 保修、活动、促销、包邮、发票、通用规则

只输出 JSON，不要解释。格式：
{{"intent": "类别", "confidence": 0.0~1.0}}

示例：
Q: ZP1 现在有货吗？ → {{"intent": "product_query", "confidence": 0.95}}
Q: 保修多久？ → {{"intent": "policy_query", "confidence": 0.9}}
Q: 我的快递怎么还没到 → {{"intent": "order_query", "confidence": 0.93}}

用户问题：{query}
JSON："""

        result = get_llm_provider().chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # 分类任务低温度
            max_tokens=80,
        )

        reply = result["reply"].strip()
        # 兼容 LLM 在 JSON 前后加 ``` 或解释文字的情况
        # 先尝试提取 {...} 段
        json_match = re.search(r"\{[^{}]+\}", reply)
        if not json_match:
            raise ValueError(f"LLM 输出不含 JSON: {reply[:100]}")

        parsed = json.loads(json_match.group(0))
        intent = parsed.get("intent", "").strip()
        confidence = float(parsed.get("confidence", 0.7))

        # 校验 intent 合法
        if intent not in _VALID_INTENTS:
            raise ValueError(f"LLM 返回非法 intent: {intent}")

        # confidence 截断到 [0, 1]
        confidence = max(0.0, min(1.0, confidence))

        # V12 包装：单意图也走 intents[] 列表（与 multi 路径统一返回结构）
        return {
            "intents": [{"intent": intent, "confidence": confidence}],
            "primary": intent,
            "method": "llm",
        }

    @staticmethod
    def _llm_classify_multi(query: str) -> dict:
        """V12：LLM 多意图分类（top-K=2）。

        让 LLM 输出 JSON 数组，按 confidence 降序截断到 top-K。
        primary = confidence 最高的 intent。
        """
        prompt = f"""你是电商客服意图分类器。判断用户问题可能涉及的多个意图（按可能性从高到低排序，最多 2 个）。

4 类意图：
- order_query: 订单状态、物流、快递、发货、签收
- refund_query: 退款、退货、换货、不想要了
- product_query: 商品参数、价格、库存、SKU、推荐
- policy_query: 保修、活动、促销、包邮、发票、通用规则

只输出 JSON，不要解释。格式：
{{"intents": [{{"intent": "类别", "confidence": 0.0~1.0}}, ...]}}

示例：
Q: ZP1 现在有货吗？ → {{"intents": [{{"intent": "product_query", "confidence": 0.95}}]}}
Q: 这台电脑续航怎么样，能分期吗 → {{"intents": [{{"intent": "product_query", "confidence": 0.9}}, {{"intent": "policy_query", "confidence": 0.65}}]}}
Q: 订单怎么退款 → {{"intents": [{{"intent": "refund_query", "confidence": 0.92}}, {{"intent": "order_query", "confidence": 0.6}}]}}
Q: 保修多久？ → {{"intents": [{{"intent": "policy_query", "confidence": 0.9}}]}}

用户问题：{query}
JSON："""

        result = get_llm_provider().chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,  # 略放宽，多意图 JSON 更长
        )

        reply = result["reply"].strip()
        # V12：先尝试整段 JSON parse（LLM 严格 JSON 模式时直接成功）
        parsed: Optional[dict] = None
        try:
            parsed = json.loads(reply)
        except json.JSONDecodeError:
            # 宽松：用括号配对找最外层 {}（兼容 ```json ... ``` / 解释文字包裹）
            json_match = _find_outermost_json(reply)
            if json_match:
                try:
                    parsed = json.loads(json_match)
                except json.JSONDecodeError as e:
                    raise ValueError(f"LLM JSON 解析失败: {e} · raw={reply[:100]}")
        if parsed is None:
            raise ValueError(f"LLM 输出不含 JSON: {reply[:100]}")

        raw_intents = parsed.get("intents", [])
        if not isinstance(raw_intents, list) or not raw_intents:
            raise ValueError(f"LLM intents 字段非法: {raw_intents}")

        intents: list[dict] = []
        for item in raw_intents:
            if not isinstance(item, dict):
                continue
            intent = str(item.get("intent", "")).strip()
            if intent not in _VALID_INTENTS:
                continue
            try:
                conf = float(item.get("confidence", 0.7))
            except (TypeError, ValueError):
                conf = 0.7
            conf = max(0.0, min(1.0, conf))
            intents.append({"intent": intent, "confidence": conf})
            if len(intents) >= TOP_K:  # 提前截断
                break

        if not intents:
            raise ValueError(f"LLM 解析后 intents 为空: {raw_intents}")

        # 按 confidence 降序
        intents.sort(key=lambda x: x["confidence"], reverse=True)

        return {
            "intents": intents,
            "primary": intents[0]["intent"],
            "method": "llm",
        }
