"""
hallucination_check.py - 真幻觉校验模块

按 2026-07-18 用户反馈整改：
- 旧版 hallucination_free = "脚本没崩"（伪指标）
- 新版：正则抽取 Agent 输出中的实体（订单号/金额/状态），与 mock DB 数据对比

校验规则：
1. 订单号：Agent 输出中的 ORD\d{8}[A-Z0-9]{3,6} 必须 ∈ 用户订单数据
2. 金额：Agent 输出中的金额（如 "199元"/"¥299"）必须 ∈ 用户订单金额集合
3. 状态：Agent 输出中的中文状态（如 "已签收"）必须 与用户订单实际状态匹配
"""
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# =============================================================
# 正则定义（与 backend/app/services/intent_service.py 对齐）
# =============================================================
ORDER_NO_RE = re.compile(r"ORD\d{8}[A-Z0-9]{3,6}", re.IGNORECASE)

# 金额正则（"199元" / "¥299" / "199.00 元"）
#   严格语义：金额必须有 ¥/￥ 前缀 OR 元 后缀 才识别，避免错把 order_no 数字当金额。
#   V10-D 同步：与 backend/app/services/validation/hallucination_guard.py 保持一致。
AMOUNT_RE = re.compile(
    r"(?:[¥￥]\s*(\d+(?:\.\d+)?))|(?:(\d+(?:\.\d+)?)\s*元)"
)

# 金额上下文：紧邻"小时/时/天/日/分钟/分/秒/个/件/月/分"等时间/单位词，
# 应判为时间/数量而非金额（V10 修复：误判"72小时"为 fake_amount）。
AMOUNT_CONTEXT_RE = re.compile(
    r"(小时|时(?!元)|天(?!无)|日|分钟|分(?!期)|秒|个|件|月)"
)

# 中文状态 → enum 值映射
STATUS_CN_TO_EN = {
    "待支付": "pending",
    "待发货": "paid",
    "已发货": "shipped",
    "运输中": "shipped",
    "已签收": "delivered",
    "已收货": "delivered",
    "已完成": "completed",
    "已退款": "refunded",
    "已退货": "refunded",
}

# 提取所有可能的中文状态
STATUS_CN_PATTERN = re.compile("|".join(re.escape(s) for s in STATUS_CN_TO_EN.keys()))


# =============================================================
# 数据结构
# =============================================================
@dataclass
class HallucinationReport:
    """幻觉校验结果"""
    has_hallucination: bool = False
    hallucination_details: List[Dict[str, Any]] = field(default_factory=list)
    extracted_entities: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================
# 校验函数
# =============================================================
def _extract_amounts_with_context(text: str) -> List[str]:
    """提取金额（取两个 capture group 中的非空值），并剔除紧邻时间/单位词的项。

    V10 修复：旧正则 `[¥￥]?\\s*(\\d+)\\s*元?` 无上下文约束，把"72小时"误判为金额，
    进一步误标为 fake_amount。增强：紧邻"小时/天/分/..."则跳过。
    """
    raw: List[str] = []
    for m in AMOUNT_RE.finditer(text):
        g = m.group(1) or m.group(2)
        if g:
            raw.append(g)

    filtered: List[str] = []
    for amt, span in zip(raw, AMOUNT_RE.finditer(text)):
        start, end = span.span()
        # 看后面 4 个字符（含匹配自身）是否紧邻时间/单位词
        tail = text[end:end + 3]
        if AMOUNT_CONTEXT_RE.search(tail):
            continue
        filtered.append(amt)
    return list(set(filtered))


def extract_entities(text: str) -> Dict[str, List[str]]:
    """从文本中抽取实体（订单号/金额/状态）。"""
    if not text:
        return {"order_nos": [], "amounts": [], "statuses": []}
    return {
        "order_nos": list(set(ORDER_NO_RE.findall(text))),
        "amounts": _extract_amounts_with_context(text),
        "statuses": list(set(STATUS_CN_PATTERN.findall(text))),
    }


def check_hallucination(
    agent_output: str,
    user_orders: List[Any],
) -> HallucinationReport:
    """真幻觉校验：Agent 输出 vs mock DB。

    Args:
        agent_output: Agent / System 生成的最终文本
        user_orders: 该用户的所有 mock 订单（含 order_no, status, total_amount）

    Returns:
        HallucinationReport: 含 has_hallucination + 幻觉细节
    """
    if not agent_output or not user_orders:
        return HallucinationReport(
            has_hallucination=False,
            hallucination_details=[],
            extracted_entities={},
        )

    details: List[Dict[str, Any]] = []
    extracted = extract_entities(agent_output)

    # 1. 订单号校验：抽出的 order_no 必须 ∈ 用户订单集合
    valid_order_nos = {o.order_no.upper() for o in user_orders}
    for on in extracted["order_nos"]:
        if on.upper() not in valid_order_nos:
            details.append({
                "type": "fake_order_no",
                "value": on,
                "valid_options": sorted(valid_order_nos),
            })

    # 2. 金额校验：抽出的金额必须 ∈ 用户订单金额集合
    valid_amounts_float = {float(o.total_amount) for o in user_orders}
    valid_amounts_str = {f"{o.total_amount:g}" for o in user_orders} | {str(int(o.total_amount)) for o in user_orders}
    for amt in extracted["amounts"]:
        try:
            amt_float = float(amt)
            if amt_float not in valid_amounts_float and amt not in valid_amounts_str:
                # 排除常见的非订单金额（如 "7天"、"24小时"）
                if amt_float < 30 or amt_float > 10000:
                    continue
                details.append({
                    "type": "fake_amount",
                    "value": amt,
                    "valid_options": sorted(valid_amounts_float),
                })
        except ValueError:
            pass

    # 3. 状态校验：抽出的中文状态必须与用户订单实际状态一致
    valid_statuses_en = {o.status for o in user_orders if hasattr(o, "status")}
    valid_statuses_en.update({o.status.value if hasattr(o.status, "value") else o.status for o in user_orders})
    for cn in extracted["statuses"]:
        en = STATUS_CN_TO_EN.get(cn)
        if en and en not in valid_statuses_en:
            details.append({
                "type": "fake_status",
                "value": cn,
                "mapped_to": en,
                "valid_options": sorted(valid_statuses_en),
            })

    return HallucinationReport(
        has_hallucination=len(details) > 0,
        hallucination_details=details,
        extracted_entities=extracted,
    )


# =============================================================
# 批量统计
# =============================================================
@dataclass
class HallucinationStats:
    total: int = 0
    hallucinated: int = 0
    fake_order_no_count: int = 0
    fake_amount_count: int = 0
    fake_status_count: int = 0

    @property
    def rate(self) -> float:
        return self.hallucinated / self.total if self.total else 0.0

    def add(self, report: HallucinationReport) -> None:
        self.total += 1
        if report.has_hallucination:
            self.hallucinated += 1
            for d in report.hallucination_details:
                if d["type"] == "fake_order_no":
                    self.fake_order_no_count += 1
                elif d["type"] == "fake_amount":
                    self.fake_amount_count += 1
                elif d["type"] == "fake_status":
                    self.fake_status_count += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "hallucinated": self.hallucinated,
            "hallucination_rate": round(self.rate, 4),
            "by_type": {
                "fake_order_no": self.fake_order_no_count,
                "fake_amount": self.fake_amount_count,
                "fake_status": self.fake_status_count,
            },
        }


# =============================================================
# CLI
# =============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # 简单测试
    class _MockOrder:
        def __init__(self, order_no, status, total_amount):
            self.order_no = order_no
            self.status = status
            self.total_amount = total_amount

    orders = [
        _MockOrder("ORD20260718001", "delivered", 299.0),
        _MockOrder("ORD20260718002", "shipped", 199.0),
    ]

    # Case 1: 无幻觉
    out1 = "您的订单 ORD20260718001 已签收，金额 299 元。"
    r1 = check_hallucination(out1, orders)
    print(f"Case 1 (无幻觉): has={r1.has_hallucination} details={r1.hallucination_details}")

    # Case 2: 假订单号
    out2 = "您的订单 ORD99999999XXX 已签收。"
    r2 = check_hallucination(out2, orders)
    print(f"Case 2 (假订单号): has={r2.has_hallucination} details={r2.hallucination_details}")

    # Case 3: 假金额
    out3 = "您的订单 ORD20260718001 已签收，金额 500 元。"
    r3 = check_hallucination(out3, orders)
    print(f"Case 3 (假金额): has={r3.has_hallucination} details={r3.hallucination_details}")

    # Case 4: 假状态
    out4 = "您的订单 ORD20260718001 已退款。"
    r4 = check_hallucination(out4, orders)
    print(f"Case 4 (假状态): has={r4.has_hallucination} details={r4.hallucination_details}")