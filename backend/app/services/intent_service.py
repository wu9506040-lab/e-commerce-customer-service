"""
Intent Service（M3 新增）

按 PROJECT_DESIGN.md §3 + §7：
- 4 类意图：order_query / refund_query / product_query / policy_query
- 三级 fallback：规则（关键词+正则）→ LLM 兜底 → 默认 policy_query
- 同时抽取实体（order_no / sku）

M3 阶段接入点：仅独立 /intent 端点，不动 /chat（M4 整合）。

性能要求 §9：/intent 响应 < 100ms（规则命中时几乎无开销）
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
    """意图分类服务（规则优先 + LLM 兜底）"""

    @staticmethod
    def classify(query: str, last_intent: Optional[IntentType] = None) -> dict:
        """
        分类入口

        Args:
            query: 用户问题
            last_intent: 上一轮意图（V2.6 启用；当前阶段透传不影响分类）

        Returns:
            {
                "intent": str,
                "confidence": float,
                "method": "rule" | "llm" | "default",
                "entities": {"order_no": str|None, "sku": str|None, "keywords": list[str]},
            }
        """
        # 1. 规则匹配
        rule_result = IntentService._rule_classify(query)
        if rule_result:
            rule_result["entities"] = IntentService._extract_entities(query)
            logger.info(f"intent(rule): {rule_result['intent']} query={query[:30]}")
            return rule_result

        # 2. LLM 兜底
        try:
            llm_result = IntentService._llm_classify(query)
            llm_result["entities"] = IntentService._extract_entities(query)
            logger.info(f"intent(llm): {llm_result['intent']} conf={llm_result['confidence']:.2f}")
            return llm_result
        except Exception as e:
            logger.warning(f"intent llm fallback 失败: {e}")

        # 3. 默认 policy_query（兜底兜底）
        return {
            "intent": "policy_query",
            "confidence": 0.5,
            "method": "default",
            "entities": IntentService._extract_entities(query),
        }

    # ---------- 私有 ----------

    @staticmethod
    def _rule_classify(query: str) -> Optional[dict]:
        """规则匹配：命中即返回"""
        for intent, patterns in INTENT_RULES.items():  # dict 保序 → 与原 tuple 列表顺序一致
            for pattern in patterns:
                if re.search(pattern, query, re.IGNORECASE):
                    return {
                        "intent": intent,
                        "confidence": 1.0,
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
        """LLM 兜底分类（few-shot）"""
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
        if intent not in ("order_query", "refund_query", "product_query", "policy_query"):
            raise ValueError(f"LLM 返回非法 intent: {intent}")

        # confidence 截断到 [0, 1]
        confidence = max(0.0, min(1.0, confidence))

        return {
            "intent": intent,
            "confidence": confidence,
            "method": "llm",
        }