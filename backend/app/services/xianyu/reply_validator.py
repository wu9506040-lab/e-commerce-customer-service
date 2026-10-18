"""
ReplyValidator — LLM 回复数据校验管道（V18-P0，纯正则，零 LLM 开销）

设计：发送前最后一道关，与 MessageGuard（敏感词）互补——Guard 管"不能说的话"，
Validator 管"格式/残留/长度/幻觉数字"：

  1. markdown 残留剥离：** __ ## ` 列表符（IM 里是乱码）
  2. RAG 引用角标剥离：[1][2]（纯数字角标，不伤 [skip]）
  3. 占位符残留：{nick}/{xxx} 字面量 → 删除（或替换真实昵称）；裸 None/null → 删
  4. 长度硬校验：>200 字按句界截到 ~120 字（prompt 约束失效时的运行时保险丝）
  5. 价格幻觉（观察模式）：回复中的 ¥金额 不在商品价格白名单 → 告警日志
     （默认不改写——赠品价值/新机对比等场景误报多，先观察一周再收紧）

Returns: (cleaned_text, issues)——issues 供日志/统计。
"""
from __future__ import annotations

import re

# markdown 残留
_MD_PATTERNS = [
    (re.compile(r"\*\*([^*]*)\*\*"), r"\1"),      # **加粗** → 文本
    (re.compile(r"\*([^*\n]+)\*"), r"\1"),        # *斜体* → 文本
    (re.compile(r"^#{1,4}\s*", re.MULTILINE), ""),  # 标题井号
    (re.compile(r"`{1,3}([^`]*)`{1,3}"), r"\1"),  # 代码标记
    (re.compile(r"^\s*[-*•]\s+", re.MULTILINE), ""),  # 列表符
]
_CITATION_RE = re.compile(r"\s*\[\d{1,2}\]")
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
_NONE_LITERAL_RE = re.compile(r"^(None|null|undefined|nan)$", re.IGNORECASE)
_PRICE_RE = re.compile(r"[¥￥]\s*([\d,]+(?:\.\d+)?)")
# 长度保险丝
_HARD_MAX = 200
_SOFT_TARGET = 120

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?…])")


def _sentence_trim(text: str, target: int) -> str:
    """按句界截到 target 字以内（保完整句）"""
    parts = _SENT_SPLIT_RE.split(text)
    out = ""
    for p in parts:
        if len(out) + len(p) > target:
            break
        out += p
    return (out or text[:target]).strip()


def validate_reply(
    text: str,
    nick: str | None = None,
    allowed_prices: set[str] | None = None,
) -> tuple[str, list[str]]:
    """校验并清洗 LLM 回复。

    Args:
        text: 原始回复
        nick: 买家真实昵称（有则 {nick} 占位符替换为昵称，无则删除）
        allowed_prices: 价格白名单（如 {"99999"}）；None = 关闭价格校验

    Returns:
        (cleaned, issues)  issues 为命中的校验项列表（日志用）
    """
    issues: list[str] = []
    t = (text or "").strip()
    if not t:
        return t, issues

    # 1. markdown 残留
    for pat, repl in _MD_PATTERNS:
        t2 = pat.sub(repl, t)
        if t2 != t:
            issues.append("markdown_stripped")
            t = t2
    # 行内话题井号残留（#售后# / #话题#）——不伤 C#、不伤纯数字
    t2 = re.sub(r"#(\S{1,12})#", r"\1", t)
    if t2 != t:
        issues.append("inline_hash_stripped")
        t = t2

    # 2. RAG 引用角标
    t2 = _CITATION_RE.sub("", t)
    if t2 != t:
        issues.append("citation_stripped")
    t = t2

    # 3. 占位符残留
    if _PLACEHOLDER_RE.search(t):
        repl = nick if nick else ""
        t = _PLACEHOLDER_RE.sub(repl, t)
        issues.append(f"placeholder_removed(nick={bool(nick)})")
    if _NONE_LITERAL_RE.match(t.strip()):
        issues.append("none_literal")
        t = ""

    # 4. 长度保险丝
    if len(t) > _HARD_MAX:
        t = _sentence_trim(t, _SOFT_TARGET)
        issues.append(f"length_trimmed_to_{len(t)}")

    # 5. 价格幻觉（决策1=A 观察模式：只告警不改写）
    if allowed_prices:
        checked: set[str] = set()
        # 5a. ¥金额（原有模式）
        for m in _PRICE_RE.finditer(t):
            num = m.group(1).replace(",", "").rstrip(".")
            if num not in allowed_prices and num not in checked:
                checked.add(num)
                issues.append(f"price_hallucination_warning:{num}")
        # 5b. 裸数字价格（决策后新增：抓"新机才 1500"这类无 ¥ 符号的幻觉）
        #     两类模式，均要求 ≥500（避开赠品 80+/单买 49 这类合法小额）：
        #     - 数字 + 元/块
        #     - 价格语境词（新机/原价/专柜/官方价/市场价/卖/值）+ 数字
        for m in re.finditer(r"(\d[\d,]{2,})\s*(?:元|块)", t):
            num = m.group(1).replace(",", "")
            if len(num.lstrip("0")) >= 3 and int(num) >= 500 and num not in allowed_prices and num not in checked:
                checked.add(num)
                issues.append(f"price_hallucination_warning:{num}元")
        for m in re.finditer(r"(?:新机|原价|专柜|官方价|市场价|零售价|值)\s*([\d,]{3,})", t):
            num = m.group(1).replace(",", "")
            if len(num.lstrip("0")) >= 3 and int(num) >= 500 and num not in allowed_prices and num not in checked:
                checked.add(num)
                issues.append(f"price_hallucination_warning:{num}(裸)")
        # 观察模式不改写——攒一周真实数据再决定收紧（删句/替换）

    t = re.sub(r"\n{2,}", "\n", t).strip()
    return t, issues


def extract_allowed_prices(item_desc: str) -> set[str]:
    """从【商品信息】JSON 里提取价格白名单（price_range 的数字部分）"""
    import json as _json
    prices: set[str] = set()
    try:
        d = _json.loads(item_desc)
        for m in _PRICE_RE.finditer(str(d.get("price_range", ""))):
            num = m.group(1).replace(",", "").rstrip(".")
            if num:
                prices.add(num)
        for sku in d.get("sku_details", []) or []:
            p = sku.get("price")
            if p:
                prices.add(str(round(float(p), 2)).rstrip(".").rstrip("."))
    except Exception:
        pass
    return prices


__all__ = ["validate_reply", "extract_allowed_prices"]
