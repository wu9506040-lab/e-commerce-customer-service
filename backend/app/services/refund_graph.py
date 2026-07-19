"""退款流程 - LangGraph V3 (M14 V3 重构 · 真实客服工作流)

真实场景建模（参考淘宝/京东客服）：
- 客服进入会话时，后台已拿到 user_id + 客户最近订单 + 历史对话上下文
- 客服/Agent 是"看着已有信息决策"，不是"现场调用工具查"
- 决策路径 4 类：
    * synthesize     - 答案明确（7 天无理由 / 已退款 / 订单不存在等）
    * need_more_info - 需要用户补全凭证/信息
    * need_confirm_order - N 单需用户点选
    * escalate       - 转人工（P0 投诉/赔付/质量/用户主动要求等）

StateGraph 4 节点：
  decide (主决策)
    ├─ escalate   → escalate_node (产 escalate_result) → END
    ├─ fetch_policy (synthesize + policy_needed=True) → fetch_policy → synthesize → END
    └─ synthesize (其他) → synthesize → END

三层决策（decide 节点）：
  L1 硬规则前置（_apply_hard_rules）：
    - Resolver SHOW_PICKER → need_confirm_order
    - Resolver ASK_LOGIN_OR_LIST → synthesize（P0-2 修订：不打爆人工坐席）
    - Resolver NOT_FOUND → escalate P2 "复杂场景"
    - 历史承诺匹配 → escalate P1 "用户要求"
  L2 LLM 语义决策（_build_decide_prompt + get_llm_provider）：
    - 4 类 decision + confidence + escalate 详情
    - P1-2 凭证兜底：state.image_urls 非空 + LLM 要凭证 → 强制 escalate P1 "质量问题"
  L3 校验兜底（_validate_decide_output）：
    - confidence < 0.7 → 自动降级 escalate P2
    - decision 枚举非法 → escalate P2
    - synthesize 必须有 target_order_no
    - retry_count 达 3 → escalate P2 "AI 多次异常"

与 V2 区别：
- V2: 6 节点（fetch_order → judge → fetch_policy → check_proof → escalate/synthesize）
- V3: 4 节点（decide → fetch_policy/escalate/synthesize），judge 提到 RefundFlow.run() initial_state
  （订单事实不在 LangGraph 内推理，因为客服后台已经查好）

灰度开关：settings.ENABLE_DECIDE_LLM（默认 False → 走 V2 兜底）
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, TypedDict

from langgraph.graph import END, StateGraph

from app.core.providers.llm import get_llm_provider
from app.services.config_loader import get_config_loader
from app.services.policy_service import PolicyService

# === V2 兼容层（mock path 用）===
# V3 重构后 fetch_order 节点已删除（订单查在 refund_flow.py _judge_basic_refundable），
# 但旧单测（test_synthesizer_refund.py）仍 mock `app.services.refund_graph.OrderTool`，
# 保留 unused import 让 mock 路径有效，避免一次性更新大量旧测试。
from app.tools.order_tool import OrderTool  # noqa: F401

logger = logging.getLogger(__name__)


# =============================================================
# State 定义
# =============================================================
class RefundState(TypedDict, total=False):
    """退款 LangGraph 状态（V3 重构）。

    输入（RefundFlow.run() 已注入）：
    - user_id: int
    - order_no: str             # 已由 Resolver 解析 / 用户提供
    - query: str                # 用户当前问句
    - history: list             # 多轮对话
    - context_block: str        # 上下文块（商品/订单跳转）
    - orders: list              # Resolver 注入的订单列表（每项带 status_zh）
    - order_info: dict          # 当前焦点订单（orders[0] 或 None）
    - refundable: bool          # judge 已算出
    - reason: str               # judge 已算出
    - status_zh: str            # 中文订单状态（Resolver/decision 注入）
    - days_since_order: int
    - resolver_result: dict     # OrderContextResolver.resolve() 返回
    - decide_retry_count: int   # LLM 异常累计（仅 LLM 异常累计）
    - dialog_turn_count: int    # 用户对话轮数（独立计数）
    - image_urls: list          # 用户上传凭证 URL（P1-2 兜底用）

    Node 输出：
    - decide_result: dict       # decide 节点产出 {decision, confidence, target_order_no, reason,
                                #   escalate, need_info, reply_key_points, policy_needed}
    - policy_docs: list         # fetch_policy 召回的条款
    - escalate_result: dict     # escalate 节点产出 {priority, category, handoff_summary}
    - final_answer: str         # synthesize 产出最终回答
    """

    # === 输入字段 ===
    user_id: int
    order_no: Optional[str]
    query: str
    user_proof: dict
    history: list
    context_block: str
    orders: list
    order_info: dict
    refundable: bool
    reason: str
    status_zh: str
    days_since_order: int
    resolver_result: dict
    decide_retry_count: int
    dialog_turn_count: int
    image_urls: list

    # === Node 输出字段 ===
    decide_result: dict
    policy_docs: list
    escalate_result: dict
    final_answer: str


# =============================================================
# 业务规则加载（启动期一次）
# =============================================================
_RULES = get_config_loader().load("decide")

CONFIDENCE_THRESHOLD: float = float(_RULES["CONFIDENCE_THRESHOLD"])
MAX_LLM_RETRIES: int = int(_RULES["MAX_LLM_RETRIES"])
STATUS_ZH_MAP: dict[str, str] = dict(_RULES["STATUS_ZH_MAP"])
_HARD_RULES_BY_ID: dict[str, dict[str, Any]] = {r["id"]: r for r in _RULES["HARD_RULES"]}
_IMAGE_URLS_OVERRIDE: dict[str, Any] = _RULES["IMAGE_URLS_OVERRIDE"]
POLICY_QUOTE_REQUIRED: bool = bool(_RULES.get("POLICY_QUOTE_REQUIRED", False))

# 决策枚举（LLM 输出和硬规则共用）
VALID_DECISIONS = ("synthesize", "need_more_info", "need_confirm_order", "escalate")

# === 兼容旧测试：refund_config.py 仍引用这两个常量 ===
# 真实业务规则加载源（与 refund_flow.py _REFUND_RULES 共用同一份 YAML）
_REFUND_RULES = get_config_loader().load("refund")
REFUND_WINDOW_DAYS: int = int(_REFUND_RULES["REFUND_WINDOW_DAYS"])
DELIVERY_OFFSET_DAYS: int = int(_REFUND_RULES["DELIVERY_OFFSET_DAYS"])


# =============================================================
# L1 硬规则前置
# =============================================================
def _apply_hard_rules(state: RefundState) -> Optional[dict]:
    """硬规则前置：命中则跳过 LLM，直接返回 decide_result dict；未命中返回 None。

    4 条硬规则（P0-2 修订）：
    1. Resolver SHOW_PICKER → need_confirm_order（复用 Resolver 决策）
    2. Resolver ASK_LOGIN_OR_LIST → synthesize（0 单降级为引导，不打爆人工坐席）
    3. Resolver NOT_FOUND → escalate P2 "复杂场景"
    4. 历史承诺匹配 → escalate P1 "用户要求"（之前 assistant 已说"已为您转接人工"等）

    Returns:
        decide_result dict（与 LLM 输出一致结构）或 None（未命中，需走 LLM）
    """
    resolver = state.get("resolver_result") or {}

    # 规则 1: SHOW_PICKER → need_confirm_order
    if resolver.get("action") == "show_picker":
        return _build_hard_rule_result("show_picker", state)

    # 规则 2: ASK_LOGIN_OR_LIST → synthesize（0 单引导用户）
    if resolver.get("action") == "ask_login_or_list":
        return _build_hard_rule_result("ask_login_or_list", state)

    # 规则 3: NOT_FOUND → escalate P2 "复杂场景"
    if resolver.get("action") == "not_found":
        return _build_hard_rule_result("not_found", state)

    # 规则 4: 历史承诺匹配（assistant 历史中说过"转人工"类）
    if _has_history_commitment(state):
        return _build_hard_rule_result("history_commitment", state)

    return None


def _build_hard_rule_result(rule_id: str, state: RefundState) -> dict:
    """根据硬规则配置构造 decide_result dict。

    Args:
        rule_id: 硬规则 ID（show_picker / ask_login_or_list / not_found / history_commitment）
        state: 当前状态（用于 reply_key_points 拼接）

    Returns:
        decide_result dict
    """
    cfg = _HARD_RULES_BY_ID[rule_id]
    decision = cfg["decision"]
    result: dict[str, Any] = {
        "decision": decision,
        "confidence": cfg.get("confidence", 1.0),
        "target_order_no": state.get("order_no"),
        "reason": f"硬规则命中: {rule_id}",
        "need_info": {"enabled": False},
        "reply_key_points": list(cfg.get("reply_key_points", [])),
        "policy_needed": cfg.get("policy_needed", False),
    }
    if decision == "escalate":
        result["escalate"] = {
            "enabled": True,
            "priority": cfg.get("priority"),
            "category": cfg.get("category"),
            "handoff_summary": f"硬规则触发: {rule_id}",
        }
    else:
        result["escalate"] = {"enabled": False}
    return result


def _has_history_commitment(state: RefundState) -> bool:
    """检测历史对话中是否含"承诺转人工"承诺（如 "已为您转接人工客服"）。

    设计点：客服之前如果说"已为您转接人工"，下一轮用户问"那能退吗"时必须 escalate P1
    （履约承诺，不能让 Agent 重新答）。
    """
    history = state.get("history") or []
    commitment_phrases = (
        "已为您转接人工",
        "已升级人工",
        "已转人工",
        "为您转接",
        "升级人工",
    )
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        content = (msg.get("content") or "").strip()
        if any(p in content for p in commitment_phrases):
            return True
    return False


# =============================================================
# L2 LLM 决策（prompt + 调用）
# =============================================================
def _build_decide_prompt(state: RefundState) -> str:
    """构造 LLM 决策 prompt。

    输入注入：
    - query: 用户当前问句
    - order_info / status_zh / refundable / reason / days_since_order
    - history: 最近 6 轮对话
    - image_urls: 用户上传凭证（标记而已，不展示）

    输出要求（JSON Schema）：
    {
      "decision": "synthesize" | "need_more_info" | "need_confirm_order" | "escalate",
      "confidence": 0.0-1.0,
      "target_order_no": str | null,
      "reason": str,
      "escalate": {"enabled": bool, "priority": "P0"|"P1"|"P2", "category": str, "handoff_summary": str},
      "need_info": {"enabled": bool, "fields": [str]},
      "reply_key_points": [str],
      "policy_needed": bool
    }
    """
    query = state.get("query", "")
    order_info = state.get("order_info") or {}
    status_zh = state.get("status_zh", "未知")
    refundable = state.get("refundable", False)
    reason = state.get("reason", "")
    days = state.get("days_since_order", 0)
    order_no = order_info.get("order_no") or state.get("order_no") or "未知"

    history = state.get("history") or []
    history_lines: list[str] = []
    for msg in history[-6:]:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            history_lines.append(f"用户: {content}")
        elif role == "assistant":
            history_lines.append(f"客服: {content}")
    history_block = "\n".join(history_lines) if history_lines else "（无历史对话）"

    image_urls = state.get("image_urls") or []
    has_proof = "已上传" if image_urls else "未上传"

    prompt = (
        "你是电商客服决策模块。请基于【事实陈述】决定下一步动作，并严格输出 JSON。\n\n"
        "【决策枚举】\n"
        "- synthesize: 答案明确，可直接回复用户\n"
        "- need_more_info: 需要用户补全凭证/故障现象等信息\n"
        "- need_confirm_order: 用户提到多笔订单/不明确，需用户点选\n"
        "- escalate: 转人工（P0 投诉/赔付/P1 质量/P1 用户要求）\n\n"
        "【事实陈述】(最高优先级)\n"
        f"订单号: {order_no}\n"
        f"订单状态: {status_zh}\n"
        f"可否退款: {'是' if refundable else '否'}\n"
        f"原因: {reason}\n"
        f"已下单 {days} 天\n"
        f"用户凭证: {has_proof}\n\n"
        "【对话历史】\n"
        f"{history_block}\n\n"
        f"用户当前问题: {query}\n\n"
        "【输出要求】\n"
        "请严格输出以下 JSON Schema（不要 Markdown 代码块标记，不要任何额外文字）：\n"
        "{\n"
        '  "decision": "synthesize" | "need_more_info" | "need_confirm_order" | "escalate",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "target_order_no": "ORD..." | null,\n'
        '  "reason": "决策原因（中文简短）",\n'
        '  "escalate": {"enabled": bool, "priority": "P0"|"P1"|"P2", "category": "投诉/质量问题/补偿诉求/用户要求/复杂场景", "handoff_summary": "一句话摘要"},\n'
        '  "need_info": {"enabled": bool, "fields": ["凭证照片", "故障视频"]},\n'
        '  "reply_key_points": ["关键回复点1", "关键回复点2"],\n'
        '  "policy_needed": bool\n'
        "}"
    )
    return prompt


def _parse_llm_json(raw: str) -> Optional[dict]:
    """解析 LLM 返回的 JSON 字符串（容错 4 级）。

    容错策略（按优先级）：
    - L1：直接 json.loads（首选，处理标准 JSON）
    - L2：ast.literal_eval（处理 Python 字面量：True/False/None 替代 true/false/null）
          这条路径兼容单测 mock 模式 `str(dict).replace("'", '"')`
    - L3：剥 Markdown 代码块（```json ... ```）后再尝试 L1/L2
    - L4：截取第一个 { 到最后一个 } 再尝试 L1/L2
    - 都失败返回 None（调用方判定为 LLM 异常 → retry_count++）

    Returns:
        解析后的 dict 或 None
    """
    import ast

    if not raw or not isinstance(raw, str):
        return None

    # 预处理：替换 Python 字面量为 JSON 字面量（让 str(dict) 也能解析）
    def _normalize_literals(s: str) -> str:
        """True/False/None → true/false/null（仅替换 word boundary 内的）。"""
        import re
        s = re.sub(r"\bTrue\b", "true", s)
        s = re.sub(r"\bFalse\b", "false", s)
        s = re.sub(r"\bNone\b", "null", s)
        return s

    def _try_parse(s: str) -> Optional[dict]:
        # 先 json.loads
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        # 再 ast.literal_eval
        try:
            data = ast.literal_eval(s)
            if isinstance(data, dict):
                return data
        except (ValueError, SyntaxError):
            pass
        return None

    normalized = _normalize_literals(raw)

    # L1/L2: 直接尝试
    result = _try_parse(normalized)
    if result is not None:
        return result

    # L3: 剥 Markdown 代码块
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if len(lines) >= 2:
            cleaned = "\n".join(lines[1:])
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        normalized_cleaned = _normalize_literals(cleaned.strip())
        result = _try_parse(normalized_cleaned)
        if result is not None:
            return result

    # L4: 截取第一个 { 到最后一个 }
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        normalized_sub = _normalize_literals(raw[start : end + 1])
        result = _try_parse(normalized_sub)
        if result is not None:
            return result

    return None


# =============================================================
# L3 校验 + 兜底
# =============================================================
def _validate_decide_output(output: dict) -> tuple[bool, dict]:
    """校验 LLM 决策输出，返回 (valid, validated)。

    校验规则：
    1. decision 枚举必须 ∈ VALID_DECISIONS（否则 valid=False → 兜底 escalate P2）
    2. confidence < CONFIDENCE_THRESHOLD → valid=True 但 decision 改为 escalate（自动降级）
    3. decision=synthesize 必须有 target_order_no（否则 valid=False）
    4. escalate.enabled=True 时 priority 必须 ∈ {P0,P1,P2}（不在则降级 P2）

    Returns:
        (valid: bool, validated: dict)
    """
    validated = dict(output)  # 拷贝避免污染原对象

    # 默认补全字段
    validated.setdefault("decision", "escalate")
    validated.setdefault("confidence", 0.5)
    validated.setdefault("target_order_no", None)
    validated.setdefault("reason", "")
    validated.setdefault("escalate", {"enabled": False})
    validated.setdefault("need_info", {"enabled": False})
    validated.setdefault("reply_key_points", [])
    validated.setdefault("policy_needed", False)

    # 1. decision 枚举校验
    decision = validated.get("decision")
    if decision not in VALID_DECISIONS:
        # 非法枚举 → 兜底 escalate P2
        validated["decision"] = "escalate"
        validated["escalate"] = {
            "enabled": True,
            "priority": "P2",
            "category": "复杂场景",
            "handoff_summary": f"LLM 输出非法 decision: {decision}",
        }
        return False, validated

    # 2. 低置信度 → 自动降级 escalate P2
    confidence = validated.get("confidence", 1.0)
    if isinstance(confidence, (int, float)) and confidence < CONFIDENCE_THRESHOLD:
        validated["decision"] = "escalate"
        validated["escalate"] = {
            "enabled": True,
            "priority": "P2",
            "category": "复杂场景",
            "handoff_summary": f"AI 置信度低({confidence:.2f})，转人工复核",
        }
        return True, validated

    # 3. synthesize 必须有 target_order_no
    if decision == "synthesize" and not validated.get("target_order_no"):
        return False, validated

    # 4. escalate.priority 校验
    escalate = validated.get("escalate") or {}
    if isinstance(escalate, dict) and escalate.get("enabled"):
        priority = escalate.get("priority")
        if priority not in ("P0", "P1", "P2"):
            escalate["priority"] = "P2"
        validated["escalate"] = escalate

    return True, validated


# =============================================================
# 主决策节点（decide_node）
# =============================================================
def decide_node(state: RefundState) -> RefundState:
    """主决策节点（L1 → L2 → L3 三层）。

    Args:
        state: 当前 RefundState（RefundFlow.run() 已注入 orders / refundable / reason / image_urls 等）

    Returns:
        state delta dict，至少包含 decide_result；retry 时更新 decide_retry_count
    """
    retry_count = int(state.get("decide_retry_count") or 0)
    image_urls = state.get("image_urls") or []

    # === L1 硬规则前置 ===
    hard_rule_result = _apply_hard_rules(state)
    if hard_rule_result is not None:
        return {"decide_result": hard_rule_result}

    # === L2 LLM 决策 ===
    prompt = _build_decide_prompt(state)
    try:
        result = get_llm_provider().chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        raw = result.get("reply", "")
    except Exception as e:
        logger.warning(f"decide_node: LLM 调用异常: {e}")
        # LLM 异常 → retry_count++
        new_retry = retry_count + 1
        if new_retry >= MAX_LLM_RETRIES:
            return {
                "decide_result": {
                    "decision": "escalate",
                    "confidence": 1.0,
                    "target_order_no": state.get("order_no"),
                    "reason": f"AI 多次异常({new_retry}次)",
                    "escalate": {
                        "enabled": True,
                        "priority": "P2",
                        "category": "复杂场景",
                        "handoff_summary": f"AI 多次异常，转人工处理",
                    },
                    "need_info": {"enabled": False},
                    "reply_key_points": [],
                    "policy_needed": False,
                },
                "decide_retry_count": new_retry,
            }
        return {"decide_retry_count": new_retry}

    # 解析 LLM JSON
    parsed = _parse_llm_json(raw)
    if parsed is None:
        # 解析失败 → retry_count++
        new_retry = retry_count + 1
        if new_retry >= MAX_LLM_RETRIES:
            return {
                "decide_result": {
                    "decision": "escalate",
                    "confidence": 1.0,
                    "target_order_no": state.get("order_no"),
                    "reason": f"LLM 输出解析失败 {new_retry} 次",
                    "escalate": {
                        "enabled": True,
                        "priority": "P2",
                        "category": "复杂场景",
                        "handoff_summary": "AI 输出无法解析，转人工处理",
                    },
                    "need_info": {"enabled": False},
                    "reply_key_points": [],
                    "policy_needed": False,
                },
                "decide_retry_count": new_retry,
            }
        return {"decide_retry_count": new_retry}

    # === L3 校验 + 兜底 ===
    valid, validated = _validate_decide_output(parsed)

    # P1-2 凭证兜底：state.image_urls 非空 + LLM 要凭证 → 强制 escalate P1 "质量问题"
    if (
        _IMAGE_URLS_OVERRIDE.get("enabled")
        and image_urls
        and validated.get("decision") == "need_more_info"
        and (validated.get("need_info") or {}).get("enabled")
    ):
        # 强制升级 P1
        validated["decision"] = "escalate"
        validated["escalate"] = {
            "enabled": True,
            "priority": _IMAGE_URLS_OVERRIDE.get("priority", "P1"),
            "category": _IMAGE_URLS_OVERRIDE.get("category", "质量问题"),
            "handoff_summary": "用户已上传凭证，AI 仍要求补全，转人工复核",
        }
        valid = True  # 强制升级视为有效

    return {"decide_result": validated, "decide_retry_count": retry_count}


# =============================================================
# fetch_policy / synthesize / escalate 节点
# =============================================================
def fetch_policy(state: RefundState) -> RefundState:
    """召回政策条款（仅 synthesize + policy_needed=True 时触发）。"""
    docs = PolicyService.search_policy(state.get("query", ""), top_k=3)
    return {"policy_docs": docs or []}


def synthesize_answer(state: RefundState) -> RefundState:
    """综合答案节点（LLM 生成最终回答）。

    Prompt 注入：
    - decide_result.reply_key_points（关键回复点）
    - decide_result.target_order_no（强约束订单号）
    - policy_docs 摘录（如有）
    - history 最近 6 轮

    反幻觉 5 条铁律（与原 V2 一致）。
    """
    decide_result = state.get("decide_result") or {}
    decision = decide_result.get("decision", "synthesize")

    # 拼 policy 摘录
    policy_lines: list[str] = []
    for i, doc in enumerate((state.get("policy_docs") or [])[:3], 1):
        text = (doc.get("text", "") or "")[:200]
        if text:
            policy_lines.append(f"[{i}] {text}")
    policy_block = "\n".join(policy_lines) if policy_lines else "（无相关政策）"

    # 拼历史
    history = state.get("history") or []
    history_lines: list[str] = []
    for msg in history[-6:]:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            history_lines.append(f"用户: {content}")
        elif role == "assistant":
            history_lines.append(f"客服: {content}")
    history_block = "\n".join(history_lines) if history_lines else "（无历史对话）"

    # 提取事实
    order_info = state.get("order_info") or {}
    target_order_no = decide_result.get("target_order_no") or order_info.get("order_no") or state.get("order_no") or "未知"
    order_status = state.get("status_zh") or STATUS_ZH_MAP.get(order_info.get("status", ""), order_info.get("status", "未知"))
    order_amount = order_info.get("total_amount", "未知")
    refundable = state.get("refundable", False)
    reason = state.get("reason", "")
    days = state.get("days_since_order", 0)
    query = state.get("query", "")
    context_block = (state.get("context_block") or "").strip()
    key_points = decide_result.get("reply_key_points") or []
    key_points_str = "\n".join(f"- {p}" for p in key_points) if key_points else "（无）"

    # 决策辅助指令
    decision_instruction = {
        "synthesize": "基于事实直接给出最终答案（同意 / 拒绝 + 原因）。",
        "need_more_info": "礼貌地请用户补充凭证/信息，列出需要的 fields。",
        "need_confirm_order": "请用户确认要操作的订单（提供订单号或从列表选择）。",
        "escalate": "（兜底路径，通常不会到 synthesize）",
    }.get(decision, "基于事实直接给出最终答案。")

    context_section = f"\n【上下文】\n{context_block}\n" if context_block else ""

    # T2.2 致命问题4 政策覆盖率提升 - 反幻觉 #6：
    #   当 POLICY_QUOTE_REQUIRED=True 且 policy_docs 非空时，强制要求 LLM 直接引用政策原文
    #   （带引号，字面照搬），而非转述/概括
    policy_quote_rule = ""
    if POLICY_QUOTE_REQUIRED and (state.get("policy_docs") or []):
        policy_quote_rule = (
            "6. 必须从【政策依据】中挑选 1 句最相关的政策原文，"
            "用双引号「」包裹后嵌入回答（如：根据\"收货后 7 天内可申请无理由退款\"，您可以...）；"
            "原文不得改字、不得省略、不得意译\n\n"
        )

    prompt = (
        "你是专业的电商客服。请严格按以下规则回答：\n\n"
        "【硬约束 - 违反任何一条都视为错误回答】\n"
        "1. 必须基于【事实陈述】回答，不得编造订单号、状态、价格、日期\n"
        "2. 如果【事实陈述】与【政策依据】冲突，以【事实陈述】为准\n"
        "3. 如果【事实陈述】信息不足，直接告知用户并禁止推测\n"
        "4. 回答中出现的订单号必须与【事实陈述】中的 order_no 完全一致，禁止换单\n"
        "5. 用户问【能不能退/能退款吗】时，必须在第一句明确回答【可以退】或【不能退 + 原因】\n"
        f"{policy_quote_rule}"
        "【决策指令】\n"
        f"{decision_instruction}\n\n"
        "【关键回复点】\n"
        f"{key_points_str}\n\n"
        "【事实陈述】(最高优先级)\n"
        f"订单号: {target_order_no}\n"
        f"订单状态: {order_status}\n"
        f"订单金额: ¥{order_amount}\n"
        f"可否退款: {'是' if refundable else '否'}\n"
        f"原因: {reason}\n"
        f"已下单 {days} 天\n\n"
        "【政策依据】\n"
        f"{policy_block}\n\n"
        f"{context_section}"
        "【对话历史】\n"
        f"{history_block}\n\n"
        f"用户当前问题: {query}\n\n"
        "回答（先给结论再补充细节，禁止编造）："
    )

    result = get_llm_provider().chat(
        [{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return {"final_answer": result.get("reply", "")}


def escalate_to_human(state: RefundState) -> RefundState:
    """升级人工节点（不调 LLM，固定产出 escalate_result）。

    RefundFlow.run() 捕获后调 EscalationService.handoff() + yield meta.handoff SSE。
    """
    decide_result = state.get("decide_result") or {}
    escalate = decide_result.get("escalate") or {}

    return {
        "escalate_result": {
            "priority": escalate.get("priority", "P2"),
            "category": escalate.get("category", "复杂场景"),
            "handoff_summary": escalate.get("handoff_summary", "需要人工协助"),
        }
    }


# =============================================================
# 条件边
# =============================================================
def _should_fetch_policy(state: RefundState) -> str:
    """判断是否触发 fetch_policy 节点（仅 fetch_policy 二元路由）。

    测试语义：仅当 decision=synthesize + policy_needed=True → "fetch_policy"，
    其他情况一律 → "synthesize"（包括 escalate / need_more_info / need_confirm_order）。

    注意：escalate 路由由 `_decide_route` 函数单独处理（在 build_refund_graph 中使用），
    这里只关心 fetch_policy 一个节点，不混入 escalate 决策。

    Returns:
        "fetch_policy" | "synthesize"
    """
    decide_result = state.get("decide_result") or {}
    if decide_result.get("decision") == "synthesize" and decide_result.get("policy_needed"):
        return "fetch_policy"
    return "synthesize"


def _decide_route(state: RefundState) -> str:
    """LangGraph 完整路由（3 路：fetch_policy / synthesize / escalate）。

    实际给 add_conditional_edges 用：
    - decision=escalate → escalate 节点
    - decision=synthesize + policy_needed → fetch_policy 节点
    - 其他 → synthesize 节点

    Returns:
        "fetch_policy" | "synthesize" | "escalate"
    """
    decide_result = state.get("decide_result") or {}
    decision = decide_result.get("decision")

    if decision == "escalate":
        return "escalate"
    if decision == "synthesize" and decide_result.get("policy_needed"):
        return "fetch_policy"
    return "synthesize"


# =============================================================
# 构建图
# =============================================================
def build_refund_graph():
    """构建退款 LangGraph（V3 · 4 节点）。

    节点：decide / fetch_policy / synthesize / escalate
    入口：decide
    条件边：decide → {fetch_policy, synthesize, escalate}
    固定边：fetch_policy → synthesize
    终止：synthesize / escalate → END
    """
    workflow = StateGraph(RefundState)

    workflow.add_node("decide", decide_node)
    workflow.add_node("fetch_policy", fetch_policy)
    workflow.add_node("synthesize", synthesize_answer)
    workflow.add_node("escalate", escalate_to_human)

    workflow.set_entry_point("decide")

    workflow.add_conditional_edges(
        "decide",
        _decide_route,
        {
            "fetch_policy": "fetch_policy",
            "synthesize": "synthesize",
            "escalate": "escalate",
        },
    )

    workflow.add_edge("fetch_policy", "synthesize")
    workflow.add_edge("synthesize", END)
    workflow.add_edge("escalate", END)

    return workflow.compile()


# 单例（应用启动时编译一次）
refund_graph_app = build_refund_graph()