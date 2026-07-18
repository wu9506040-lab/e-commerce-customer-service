"""EscalationService - 转人工兜底（M14 V3）

业务诉求：Agent 异常 / 答不上来 / 用户明确要求 → 把"用户名片 + 最近订单 + 最近对话 +
当前意图 + 失败上下文"打包推到前端，人工坐席可一键接入。

为什么不是工单系统：
- MVP 范围：只生成 payload 推 SSE meta，前端展示转人工卡片即可
- 持久化工单表 / 人工坐席工作台 out of scope（面试讲设计，不写代码）
- LLM 生成摘要 out of scope（直接塞最近 5 条原文，<1s 延迟）

触发点（3 类）：
- agent_unavailable：RefundFlow/handle_refund_v3 LangGraph exception → V2 fallback
                   失败 → EscalationService.handoff(reason=AGENT_UNAVAILABLE)
- user_requested：用户输入含"转人工"关键词 → handoff(reason=USER_REQUESTED)
- business_rule：LangGraph escalate 节点（质量问题无凭证等）→ handoff(reason=BUSINESS_RULE)

灰度开关：settings.ENABLE_ESCALATION_HANDOFF（默认 False）
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.tools.order_tool import OrderTool

logger = logging.getLogger(__name__)


# =============================================================
# 枚举
# =============================================================
class EscalationReason(str, Enum):
    """触发原因"""

    AGENT_UNAVAILABLE = "agent_unavailable"  # Agent 挂了（V3 + V2 都失败）
    USER_REQUESTED = "user_requested"        # 用户说"转人工"
    BUSINESS_RULE = "business_rule"          # 业务规则（质量问题无凭证等）


_REASON_LABEL: dict[str, str] = {
    EscalationReason.AGENT_UNAVAILABLE.value: "系统繁忙，已为您升级人工客服",
    EscalationReason.USER_REQUESTED.value: "已为您转接人工客服",
    EscalationReason.BUSINESS_RULE.value: "已升级人工客服为您处理",
}


# =============================================================
# HandoffPayload（推到前端 SSE meta.handoff 字段）
# =============================================================
@dataclass
class HandoffPayload:
    """转人工 payload 数据结构

    字段说明：
    - handoff_id: 工单号（短 UUID），人工坐席凭此接入
    - reason: 触发原因枚举值
    - reason_label: 给用户看的中文标签
    - created_at: ISO 8601 时间戳
    - user_id: 用户 ID（ANONYMOUS 也 OK，user_card.total_orders=0）
    - user_card: 用户名片（基础信息 + 订单数）
    - recent_orders: 最近 N 单（默认 3），给人工看
    - recent_messages: 最近 5 条对话原文（按时间正序），给人工看上下文
    - current_intent: 当前意图（refund_query / order_query / ...）
    - current_entities: 当前意图的实体（order_no / sku）
    - agent_failure_context: 仅 AGENT_UNAVAILABLE 时填（failed_stage, error_class, retry_count）
    - summary_text: 一句话摘要（拼装，不调 LLM）
    - priority: P0/P1/P2 优先级（M14 V3+ P0 关键词命中后写入）
    - category: 中文分类（投诉 / 质量问题 / 补偿诉求 / 用户要求 / 复杂场景）
    - matched_keyword: 命中的关键词（给人工看是哪句话触发的）
    - detected_category: 类别 key（complaint / compensation / quality / user_requested / None）
    """

    handoff_id: str
    reason: str
    reason_label: str
    created_at: str
    user_id: int
    user_card: dict = field(default_factory=dict)
    recent_orders: list = field(default_factory=list)
    recent_messages: list = field(default_factory=list)
    current_intent: Optional[str] = None
    current_entities: Optional[dict] = None
    agent_failure_context: Optional[dict] = None
    summary_text: str = ""
    # M14 V3+: P0 关键词命中后写入（向后兼容：未传时默认 None）
    priority: Optional[str] = None
    category: Optional[str] = None
    matched_keyword: Optional[str] = None
    detected_category: Optional[str] = None

    def to_dict(self) -> dict:
        """转 dict（SSE JSON 序列化友好）"""
        return asdict(self)


# =============================================================
# 转人工关键词检测（轻量级路由，不污染 guard 的 3 层防御）
# =============================================================
_HANDOFF_KEYWORDS: tuple[str, ...] = (
    "转人工",
    "转接人工",
    "人工客服",
    "真人客服",
    "找人工",
    "人工服务",
    "人工处理",
    "转给人工",
    "转人工客服",
)


def detect_handoff_keyword(query: str) -> bool:
    """检测用户是否请求转人工。

    命中 → chat.py 走 escalation 路径，跳过 IntentService.classify 与 LLM 调用。
    设计点：放在 chat.py 入口层而不是 guard.py，避免污染 guard 的 3 层防御定位
    （guard 是 LLM token 防滥用，转人工是路由决策，两件事正交）。

    注意：P0 关键词（投诉/赔付/质量等）由 detect_p0_escalate 处理，不在此处拦截
    （避免重复检测）。
    """
    if not query or not isinstance(query, str):
        return False
    q = query.strip()
    return any(kw in q for kw in _HANDOFF_KEYWORDS)


# =============================================================
# P0 高风险关键词检测（M14 V3+ · 真实话术驱动）
# =============================================================
# 数据源：data/m14_validation/data/real_corpus.json 中 RC005/012/015/033/051-056/069/077/095/097 等
# 18 条真实话术里抽取的关键词命中词（覆盖电商客服"投诉/赔付/质量/转人工"4 大高风险场景）
#
# 优先级排序（P0-2 修订）：
#   COMPLAINT/COMPENSATION > QUALITY/USER_REQUESTED
#   含义：同时含"投诉"和"质量" → complaint 优先（语义上投诉 > 质量问题）
#         同时含"三倍赔偿"和"转人工" → compensation 优先（金额诉求更紧急）
ESCALATE_P0_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 投诉类（最高优先级：P0 投诉 = 公开市场监督风险）
    # 顺序：specific 数字/机构在前（"12315"/"315" 等更 actionable），通用词在后
    "complaint": (
        "12315", "12305", "315", "工商局", "市监", "投诉", "曝光",
    ),
    # 赔付类（P0 赔付 = 金额诉求 / 法规诉求）
    "compensation": (
        "三倍赔偿", "退一赔三", "假一赔十",
    ),
    # 质量类（P0 质量 = 商品质量缺陷，需要凭证流程）
    "quality": (
        "质量问题", "破损", "坏点", "开胶", "假货", "二手商品",
    ),
    # 主动要人工类（P0 = 用户明确要求升级）
    "user_requested": (
        "转人工", "转主管", "机器人", "起诉", "律师",
    ),
}

# 4 类别默认优先级与中文标签（与 config/business_rules/decide.yaml ESCALATE_CATEGORIES 对齐）
_P0_CATEGORY_PRIORITY: dict[str, str] = {
    "complaint": "P0",
    "compensation": "P0",
    "quality": "P0",
    "user_requested": "P0",
}
_P0_CATEGORY_LABEL: dict[str, str] = {
    "complaint": "投诉",
    "compensation": "补偿诉求",
    "quality": "质量问题",
    "user_requested": "用户要求",
}


def detect_p0_escalate(query: str) -> Optional[tuple[str, str]]:
    """检测用户 query 是否含 P0 高风险关键词。

    命中 → 返回 (category, matched_keyword)；
    不命中 → 返回 None。

    优先级规则：COMPLAINT/COMPENSATION > QUALITY/USER_REQUESTED。
    即 query 同时含"投诉"和"质量"时，命中 complaint（投诉更紧急）。

    命中后由 chat.py 上层调用 EscalationService.handoff() 写入 priority/category 等字段，
    本函数只做"识别"一件事，便于单测和审计追溯。

    设计点（与 detect_handoff_keyword 的区别）：
    - detect_handoff_keyword 只匹配原 9 词"转人工"类（向后兼容，保留路由作用）
    - detect_p0_escalate 扩到 4 类 23 词（含投诉/赔付/质量等高风险场景）
    - 两个函数共存：chat.py 先调 detect_p0_escalate（更精准 + 4 类优先级），未命中再调 detect_handoff_keyword（兜底）

    Args:
        query: 用户输入原文（已 strip 后的字符串）

    Returns:
        (category_key, matched_keyword) 元组 或 None
        例：detect_p0_escalate("我要投诉 12315") → ("complaint", "12315")
    """
    if not query or not isinstance(query, str):
        return None
    q = query.strip()
    if not q:
        return None
    # 按优先级遍历：COMPLAINT → COMPENSATION → QUALITY → USER_REQUESTED
    for category in ("complaint", "compensation", "quality", "user_requested"):
        for kw in ESCALATE_P0_KEYWORDS[category]:
            if kw in q:
                return (category, kw)
    return None


def get_p0_category_info(category: str) -> tuple[str, str]:
    """根据 P0 category key 取 (priority, label)。

    Args:
        category: category key（complaint / compensation / quality / user_requested）

    Returns:
        (priority, label) 元组；未知 category 返回 ("P0", category)
    """
    priority = _P0_CATEGORY_PRIORITY.get(category, "P0")
    label = _P0_CATEGORY_LABEL.get(category, category)
    return (priority, label)


# =============================================================
# 服务
# =============================================================
class EscalationService:
    """转人工服务（M14 V3 新增）"""

    def __init__(self) -> None:
        pass

    def handoff(
        self,
        reason: EscalationReason,
        user_id: int,
        history: Optional[list[dict]] = None,
        intent_result: Optional[dict] = None,
        failure_context: Optional[dict] = None,
        recent_orders: Optional[list[dict]] = None,
        priority: Optional[str] = None,
        category: Optional[str] = None,
        matched_keyword: Optional[str] = None,
        detected_category: Optional[str] = None,
    ) -> HandoffPayload:
        """生成 handoff payload

        Args:
            reason: 触发原因
            user_id: 用户 ID（ANONYMOUS 也 OK）
            history: 最近对话历史（[{role, content, ts?}, ...]）
            intent_result: 当前意图识别结果（intent, entities, ...）
            failure_context: Agent 异常上下文（failed_stage, error_class, retry_count）
            recent_orders: 已查好的最近订单列表（如已有则复用，避免重复查 DB）
            priority: M14 V3+ P0 关键词命中后写入（P0/P1/P2）
            category: 中文分类（投诉 / 质量问题 / 补偿诉求 / 用户要求 / 复杂场景）
            matched_keyword: 命中的关键词（人工坐席看是哪句话触发的）
            detected_category: 类别 key（complaint / compensation / quality / user_requested）

        Returns:
            HandoffPayload
        """
        # 1. 用户名片 + 最近订单（DB 查询失败不阻断流程）
        if recent_orders is None:
            try:
                recent_orders = OrderTool.list_user_orders(user_id, limit=3)
                # DB 异常时 OrderTool 可能返 None（warning 已 log），降级为空列表
                if recent_orders is None:
                    recent_orders = []
            except Exception as e:
                logger.warning(
                    f"EscalationService: list_user_orders failed: user_id={user_id}, {e}"
                )
                recent_orders = []

        user_card = {
            "user_id": user_id,
            "total_orders": len(recent_orders),
            "recent_order_count": len(recent_orders),
        }

        # 2. 最近对话（最近 5 条原文，按时间正序）
        recent_messages: list[dict] = []
        if history:
            recent_messages = list(history[-5:]) if len(history) > 5 else list(history)

        # 3. 当前意图 + 实体
        current_intent: Optional[str] = None
        current_entities: Optional[dict] = None
        if intent_result:
            current_intent = intent_result.get("intent")
            current_entities = intent_result.get("entities", {})

        # 4. 一句话摘要（拼装，不调 LLM）
        summary_text = self._build_summary(
            current_intent=current_intent,
            current_entities=current_entities,
            recent_messages=recent_messages,
            failure_context=failure_context,
        )

        # 5. 工单号 + 时间戳
        # 格式：H + 8 位大写 hex（易读易识别，例如 H7A3F9C2E）
        handoff_id = f"H{uuid.uuid4().hex[:8].upper()}"
        created_at = datetime.now(timezone.utc).isoformat()

        return HandoffPayload(
            handoff_id=handoff_id,
            reason=reason.value,
            reason_label=_REASON_LABEL[reason.value],
            created_at=created_at,
            user_id=user_id,
            user_card=user_card,
            recent_orders=recent_orders,
            recent_messages=recent_messages,
            current_intent=current_intent,
            current_entities=current_entities,
            agent_failure_context=failure_context,
            summary_text=summary_text,
            priority=priority,
            category=category,
            matched_keyword=matched_keyword,
            detected_category=detected_category,
        )

    # ---------- 私有 ----------

    @staticmethod
    def _build_summary(
        current_intent: Optional[str],
        current_entities: Optional[dict],
        recent_messages: list[dict],
        failure_context: Optional[dict],
    ) -> str:
        """拼装一句话摘要（不调 LLM）"""
        parts: list[str] = []
        if current_intent == "refund_query":
            parts.append("申请退款")
        elif current_intent == "order_query":
            parts.append("查询订单")
        elif current_intent == "product_query":
            parts.append("商品咨询")
        elif current_intent == "policy_query":
            parts.append("政策咨询")
        if current_entities:
            order_no = current_entities.get("order_no")
            if order_no:
                parts.append(f"订单 {order_no}")
        if recent_messages:
            last_msg = recent_messages[-1].get("content", "")
            if last_msg:
                # 截断到 30 字符（避免摘要太长）
                parts.append(f"最后说: {last_msg[:30]}")
        if failure_context:
            failed_stage = failure_context.get("failed_stage", "")
            if failed_stage:
                parts.append(f"失败阶段: {failed_stage}")
        return "，".join(parts) if parts else "（无摘要）"


# =============================================================
# 工厂入口
# =============================================================
_service: Optional[EscalationService] = None


def get_escalation_service() -> EscalationService:
    """工厂入口。业务模块**只能**通过此函数获取（禁止直接 new）。"""
    global _service
    if _service is None:
        _service = EscalationService()
    return _service


def reset_escalation_service() -> None:
    """测试钩子：重置单例（仅供 test fixtures）。"""
    global _service
    _service = None
