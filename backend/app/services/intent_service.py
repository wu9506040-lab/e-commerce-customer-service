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

from app.core.qwen import chat as qwen_chat
from app.schemas.intent import IntentEntities, IntentType

logger = logging.getLogger(__name__)


# 意图分类规则（PROJECT_DESIGN.md §7 草案 + 扩展）
# 顺序敏感：先匹配 refund（含"买"语义）→ order → product → policy 兜底
INTENT_RULES: list[tuple[IntentType, list[str]]] = [
    # refund_query：退款/退货/换货（带"我的"语义更稳，避免和"退货运费谁出"误命中）
    ("refund_query", [
        # 必须含明确个人语境（"我要/想" + 退 OR 明确动作），避免"退货运费谁出"误命中
        r"我[要想要]?退款", r"我要退货", r"我要退换", r"我要换货",
        r"想退", r"想退掉", r"想退款",
        r"不想要了",
        r"能退吗", r"可以退", r"能不能退", r"还能退吗", r"能退款吗",
        r"申请退款", r"申请退货", r"发起退款",
    ]),
    # policy_query：政策类（保修/活动/促销/包邮/发票 等通用规则 + 退货条件咨询）
    # 注意："7 天无理由"放在这 — 用户问"7 天无理由退货运费谁出"是政策咨询，不是真要退
    ("policy_query", [
        r"7\s*天无理由", r"七天无理由", r"无理由退",
        # M5 修复：发货时效 / 电池保修 — 这两类之前被 order/product 规则抢匹配
        r"什么时候发货", r"多久发货", r"几天发货", r"发货时间",
        r"电池.*保修", r"电池.*质保", r"保修多久", r"质保多久", r"保修期多久",
        # 注：去掉 r"保修" / r"质保" — 含 sku 的"ZP1 保修多久"应走 product_query
        # 未命中其他 3 类时，policy_query 作为兜底（见 _rule_classify 默认逻辑）
        r"包邮", r"邮费", r"运费", r"发票",
        r"活动", r"促销", r"折扣", r"优惠券", r"满减",
    ]),
    # order_query：订单/物流/快递/发货
    ("order_query", [
        r"我的订单", r"我的那.*订单", r"订单状态",
        r"物流", r"快递", r"到哪", r"到货", r"发货", r"派送",
        r"签收", r"运单", r"单号",
    ]),
    # product_query：商品参数/价格/库存
    ("product_query", [
        r"多少钱", r"价格", r"参数", r"配置", r"规格",
        r"续航", r"电池", r"内存", r"颜色", r"尺码", r"尺寸",
        r"有没有货", r"库存", r"在哪买", r"推荐",
        r"ZP\d", r"BP\d", r"LP\d",  # 我们的 SKU 前缀
    ]),
    # policy_query：兜底（保修/活动/促销/包邮/发票 等通用政策）
    # 放在最后：未命中前 3 类默认就是 policy_query
]

# 实体抽取正则
ORDER_NO_RE = re.compile(r"\bORD\d{3,}\b", re.IGNORECASE)
SKU_RE = re.compile(r"\b(?:ZP|BP|LP)\d{1,3}\b", re.IGNORECASE)


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
        for intent, patterns in INTENT_RULES:
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

        result = qwen_chat(
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