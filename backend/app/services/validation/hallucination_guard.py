"""
hallucination_guard.py - 生产链路反幻觉后置校验（V10-D · V11-B 升级）

定位：
- 与 scripts/m14_validation/hallucination_check.py 同源正则与字段语义
- 不依赖 scripts 路径，可被 refund_graph.synthesize_answer 在生产链路调用
- 不输出 JSON report；只返回 (cleaned_text, hits)
- 命中策略：能用真实字段替换就替换；不能替换就剥离（不回退到 escalate）

设计边界：
- 仅做"实体替换/剥离"，不改文案、不动 prompt
- 命中项通过 logger.warning 结构化落库（运营可观察）
- 返回值仍是 plain text；不强制结构化（前端按 token 处理）

V11-B 升级（2026-07-21）：
- fake_status 业务层从"仅 warning"升级为"替换/剥离"（对齐 fake_amount/fake_order_no 的双重防护）
- 复用 decide.yaml STATUS_ZH_MAP（6 个英文状态 → 中文）作为合法状态词集合
- 新增灰度开关 HALLUCINATION_REPLACE_FAKE_STATUS（默认 true，可关退回 V10-D 行为）
- 替换策略：文本中的 status_zh != 真实 order_info.status_zh → 替换为「您的订单当前状态是:{real}」
- 兜底：real status_zh 为空时降级为剥离（避免凭空编造）
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================
# V11-B：fake_status 替换所需的状态词配置（启动期加载）
# =============================================================
def _load_status_zh_map() -> dict[str, str]:
    """启动期加载 STATUS_ZH_MAP；加载失败时用 hardcode 兜底（保持服务可用）。"""
    try:
        from app.services.config_loader import get_config_loader
        rules = get_config_loader().load("decide")
        m = rules.get("STATUS_ZH_MAP") if isinstance(rules, dict) else None
        if isinstance(m, dict) and m:
            return dict(m)
    except Exception as e:  # noqa: BLE001
        logger.warning("[hallucination_guard] STATUS_ZH_MAP 加载失败,使用 hardcode 兜底: %s", e)
    # hardcode 兜底（与 decide.yaml §3 保持一致；启动期即可用）
    return {
        "pending": "待支付",
        "paid": "已支付",
        "shipped": "运输中",
        "delivered": "已签收",
        "completed": "已完成",
        "refunded": "已退款",
    }


_STATUS_ZH_MAP: dict[str, str] = _load_status_zh_map()
_VALID_STATUS_ZH: set[str] = set(_STATUS_ZH_MAP.values())  # 用于正则匹配 LLM 输出中的状态词

# V11-B 灰度开关：false → 退回 V10-D "仅 warning 不替换" 行为
try:
    from app.services.config_loader import get_config_loader
    _RULES = get_config_loader().load("decide")
    HALLUCINATION_REPLACE_FAKE_STATUS: bool = bool(
        _RULES.get("HALLUCINATION_REPLACE_FAKE_STATUS", True)
    )
except Exception:
    HALLUCINATION_REPLACE_FAKE_STATUS = True  # 默认开(沿用 V11-A 之后的策略)


# =============================================================
# 正则（与 scripts/m14_validation/hallucination_check.py 对齐，V10-D 强化）
# =============================================================
ORDER_NO_RE = re.compile(r"ORD\d{8}[A-Z0-9]{3,6}", re.IGNORECASE)

# 金额正则（"199元" / "¥299" / "199.00 元"）
#   严格语义：金额必须有 ¥/￥ 前缀 OR 元 后缀 才识别，避免错把 order_no 数字当金额。
#   V10-D 修复：旧版 `[¥￥]?\s*(\d+)\s*元?` 无上下文约束，会把 ORD99999999 中的 "99"
#   误判为金额，导致 post-subn 把订单号改坏。
AMOUNT_RE = re.compile(
    r"(?:[¥￥]\s*(\d+(?:\.\d+)?))|(?:(\d+(?:\.\d+)?)\s*元)"
)


def _extract_amounts(text: str) -> list[str]:
    """提取金额（取两个 capture group 中的非空值）。"""
    return [
        g for g in (m.group(1) or m.group(2) for m in AMOUNT_RE.finditer(text))
        if g
    ]


def _replace_amount(text: str, old_amt: str, new_amt: str) -> tuple[str, int]:
    """把 old_amt 在 text 中第一次出现的位置替换为 new_amt。

    强约束："¥old_amt" 或 "old_amt元" 上下文才替换，避免误伤 order_no。
    """
    pattern_yuan_suffix = re.compile(
        rf"{re.escape(old_amt)}\s*元",
    )
    new_text, n = pattern_yuan_suffix.subn(f"{new_amt}元", text, count=1)
    if n > 0:
        return new_text, n
    pattern_currency = re.compile(
        rf"[¥￥]\s*{re.escape(old_amt)}",
    )
    new_text, n = pattern_currency.subn(f"¥{new_amt}", text, count=1)
    return new_text, n


# =============================================================
# 公共辅助
# =============================================================
def _normalize_amount(value: Any) -> float | None:
    """把各种形态的金额归一为 float（order_info.total_amount 可能是 str/int/float）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_amount_for_display(value: float) -> str:
    """把真实金额格式化为显示串：322.21 → "322.21"，300.0 → "300"。"""
    if value == int(value):
        return f"{int(value)}"
    return f"{value:.2f}"


# =============================================================
# V11-B：fake_status 替换（状态词正则 + 替换/剥离逻辑）
# =============================================================
def _build_status_pattern() -> re.Pattern[str]:
    """用合法状态词集合构建正则。状态词按长度倒序，避免"已签收"被拆成"已"+"签收"。"""
    sorted_statuses = sorted(_VALID_STATUS_ZH, key=len, reverse=True)
    return re.compile("|".join(re.escape(s) for s in sorted_statuses))


_STATUS_PATTERN: re.Pattern[str] = _build_status_pattern()


def _extract_statuses(text: str) -> list[str]:
    """提取文本中所有合法状态词（保持出现顺序,去重按首次出现位置）。"""
    return list(dict.fromkeys(_STATUS_PATTERN.findall(text)))


def _replace_fake_status(text: str, real_status_zh: str) -> tuple[str, list[dict[str, Any]]]:
    """fake_status 业务层硬替换（V11-B）。

    策略：
      - 提取文本中所有合法状态词
      - 状态词 == real_status_zh → 保留（命中真值,不替换）
      - 状态词 != real_status_zh → 替换为「您的订单当前状态是:{real_status_zh}」

    Args:
        text: LLM 产出的最终回答文本
        real_status_zh: order_info["status_zh"] 真实状态中文(可能为空字符串)

    Returns:
        (cleaned_text, hits)
        - cleaned_text: 替换后的文本(若无命中则原样返回)
        - hits: 命中明细(每项 {"type": "fake_status_replaced", "old": ..., "new": ...})
    """
    hits: list[dict[str, Any]] = []
    cleaned = text
    if not cleaned or not _VALID_STATUS_ZH:
        return cleaned, hits

    extracted = _extract_statuses(cleaned)
    if not extracted:
        return cleaned, hits

    for status_word in extracted:
        if status_word == real_status_zh:
            continue  # 命中真值,跳过
        # 替换为「您的订单当前状态是:{real_status_zh}」(若 real_status_zh 为空则降级剥离)
        replacement = (
            f"您的订单当前状态是:{real_status_zh}"
            if real_status_zh
            else "您的订单状态"
        )
        cleaned, n = re.subn(
            re.escape(status_word),
            replacement,
            cleaned,
            count=1,
        )
        if n > 0:
            hits.append(
                {
                    "type": "fake_status_replaced",
                    "old": status_word,
                    "new": replacement,
                }
            )

    return cleaned, hits


def _strip_fake_status(text: str) -> tuple[str, list[dict[str, Any]]]:
    """fake_status 兜底:real_status_zh 为空时直接剥离状态词（V11-B 降级路径）。

    Returns:
        (cleaned_text, hits) - hits 每项 type="fake_status_stripped"
    """
    hits: list[dict[str, Any]] = []
    cleaned = text
    if not cleaned:
        return cleaned, hits
    extracted = _extract_statuses(cleaned)
    for status_word in extracted:
        cleaned, n = re.subn(re.escape(status_word), "", cleaned, count=1)
        if n > 0:
            hits.append(
                {
                    "type": "fake_status_stripped",
                    "old": status_word,
                    "reason": "no_real_status_in_context",
                }
            )
    return cleaned, hits


# =============================================================
# 主入口
# =============================================================
def post_synthesize_check(
    text: str,
    order_info: dict | None,
) -> tuple[str, list[dict[str, Any]]]:
    """对 synthesize 节点产出 final_answer 做后置反幻觉。

    Args:
        text: LLM 产出的最终回答文本
        order_info: LangGraph state["order_info"]（dict | {} | None）
            - 含 total_amount / order_no / status / status_zh 等真实字段
            - 当 OrderTool.get_order_by_no 返回 None 时为 {}（如 M14-0070）

    Returns:
        (cleaned_text, hits)
        - cleaned_text: 替换/剥离后的文本（若无命中则原样返回）
        - hits: 命中明细（list），每项是 {"type": str, ...}

    命中策略：
        - fake_amount：amount 在文本中、不在真实金额集合 → 用真实金额替换
        - fake_order_no：order_no 在文本中、与真实订单号不一致 → 用真实订单号替换；
            真实订单号也缺失时 → 剥离（避免回显用户输入的伪单号）
        - fake_status（V11-B 升级）：status_zh 在文本中、与真实状态不一致 →
            替换为「您的订单当前状态是:{real_status_zh}」；真实状态缺失时降级为剥离。
            灰度开关 HALLUCINATION_REPLACE_FAKE_STATUS=false → 退回 V10-D 行为(仅 warning)。
    """
    if not text:
        return text, []

    hits: list[dict[str, Any]] = []
    cleaned = text

    real_amount = _normalize_amount((order_info or {}).get("total_amount"))
    real_order_no = (order_info or {}).get("order_no")
    real_status_zh = (order_info or {}).get("status_zh") or ""

    # ---------- 1. 金额：替换为真实金额 ----------
    if real_amount is not None:
        extracted_amounts = list(dict.fromkeys(_extract_amounts(cleaned)))
        real_amount_str = _format_amount_for_display(real_amount)
        for amt in extracted_amounts:
            try:
                amt_float = float(amt)
            except ValueError:
                continue
            # 与真实金额误差 ≤ 0.01 视为命中真值
            if abs(amt_float - real_amount) <= 0.01:
                continue
            # 仅处理订单金额量级（30~10000），跳过 7 天 / 24 小时 等政策术语
            if amt_float < 30 or amt_float > 10000:
                continue
            # 真实金额也是同量级（防御：real=200, fake=199, 199→200 是合理替换）
            if real_amount < 30 or real_amount > 10000:
                continue
            cleaned, n = _replace_amount(cleaned, amt, real_amount_str)
            if n > 0:
                hits.append(
                    {
                        "type": "fake_amount_replaced",
                        "old": amt,
                        "new": real_amount_str,
                    }
                )

    # ---------- 2. 订单号：替换为真实订单号；缺失则剥离 ----------
    if real_order_no:
        extracted_order_nos = set(ORDER_NO_RE.findall(cleaned))
        for on in extracted_order_nos:
            if on.upper() == real_order_no.upper():
                continue
            cleaned, n = re.subn(
                re.escape(on),
                real_order_no,
                cleaned,
                count=1,
            )
            if n > 0:
                hits.append(
                    {
                        "type": "fake_order_no_replaced",
                        "old": on,
                        "new": real_order_no,
                    }
                )
    else:
        # 真实订单号缺失（order_info 为空，如 M14-0070 invalid_order）：
        # 不能凭空编造，只能剥离用户输入的伪单号，避免 synthesize 回显
        extracted_order_nos = set(ORDER_NO_RE.findall(cleaned))
        for on in extracted_order_nos:
            cleaned, n = re.subn(re.escape(on), "", cleaned, count=1)
            if n > 0:
                hits.append(
                    {
                        "type": "fake_order_no_stripped",
                        "old": on,
                        "reason": "no_real_order_in_context",
                    }
                )

    # ---------- 3. 订单状态（V11-B）：替换/剥离（与 #9 prompt 硬约束配合）----------
    # V10-D 仅 warning;V11-B 升级为业务层硬替换(fake_amount/fake_order_no 已对齐)。
    # 灰度开关 HALLUCINATION_REPLACE_FAKE_STATUS=false → 退回 V10-D 行为(仅 warning)。
    if HALLUCINATION_REPLACE_FAKE_STATUS:
        if real_status_zh:
            # 真实状态已知:替换不一致的状态词为「您的订单当前状态是:{real_status_zh}」
            cleaned, status_hits = _replace_fake_status(cleaned, real_status_zh)
            hits.extend(status_hits)
        else:
            # 真实状态缺失(order_info 为空,如 M14-0070 invalid_order):降级剥离
            cleaned, status_hits = _strip_fake_status(cleaned)
            hits.extend(status_hits)

    if hits:
        logger.warning(
            "[post_synth_hallucination_fix] hits=%d cleaned_len=%d order_no=%s",
            len(hits),
            len(cleaned),
            real_order_no or "none",
        )

    return cleaned, hits
