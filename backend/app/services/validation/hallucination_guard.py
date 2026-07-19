"""
hallucination_guard.py - 生产链路反幻觉后置校验（V10-D）

定位：
- 与 scripts/m14_validation/hallucination_check.py 同源正则与字段语义
- 不依赖 scripts 路径，可被 refund_graph.synthesize_answer 在生产链路调用
- 不输出 JSON report；只返回 (cleaned_text, hits)
- 命中策略：能用真实字段替换就替换；不能替换就剥离（不回退到 escalate）

设计边界：
- 仅做"实体替换/剥离"，不改文案、不动 prompt
- 命中项通过 logger.warning 结构化落库（运营可观察）
- 返回值仍是 plain text；不强制结构化（前端按 token 处理）
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


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
        - fake_status：仅 logger.warning，不改文本（状态术语歧义大，安全替换风险高）
    """
    if not text:
        return text, []

    hits: list[dict[str, Any]] = []
    cleaned = text

    real_amount = _normalize_amount((order_info or {}).get("total_amount"))
    real_order_no = (order_info or {}).get("order_no")

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

    if hits:
        logger.warning(
            "[post_synth_hallucination_fix] hits=%d cleaned_len=%d order_no=%s",
            len(hits),
            len(cleaned),
            real_order_no or "none",
        )

    return cleaned, hits
