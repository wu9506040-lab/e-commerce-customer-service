"""阶梯议价价格引擎（V20 亮点 6）：LLM 只管话术，价格由确定性规则控制。

为什么需要（与现有 bargain_counter 的关系）：
- bargain_counter 已解决"议价轮次计数 + 让步策略指令注入 system prompt"，
  但让多少价是 LLM 自由发挥的——LLM 报错价是要真赔钱的
  （reply_validator 的价格白名单只能拦截，不能决策）。
- 本模块把**让价决策**从 LLM 手里拿回来：底价、每轮让幅、拖底策略全部
  确定性规则；LLM 只负责把 decided 的价格说成人话。

阶梯规则（默认，per-SKU 可覆盖）：
  price 模式：轮次 1 不让 / 2 让 3% / 3 让 5% / ≥4 底价托底（底价 = 价 × (1-max_discount)）
  gift 模式（与 V18-P0.1"不二价人设"对齐）：价格不让，梯度加赠品——
    轮次 1 拒绝 / 2 送数据线 / 3 加 18W 充电头 / ≥4 赠品拉满并促单
  两种模式都由引擎确定性决策，LLM 只负责话术。

输出给 LLM 的契约：
  {"strategy": "reject|concede|floor", "price": 期望报价 or None,
   "floor_price": 底价, "round": N, "instruction": 给 LLM 的一句话约束}
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 默认阶梯：轮次 → 让价比例（相对商品原价）
DEFAULT_LADDER = {1: 0.0, 2: 0.03, 3: 0.05}


def decide_offer(
    list_price: float,
    bargain_round: int,
    max_discount: float = 0.10,
    ladder: Optional[dict[int, float]] = None,
) -> dict[str, Any]:
    """确定性议价决策。

    Args:
        list_price: 商品现价（元）
        bargain_round: 议价轮次（1 起；来自 BargainCounter.get_count）
        max_discount: 底价折扣上限（0.10 = 最低 9 折）
        ladder: 轮次→让价比例覆盖表（per-SKU 可配）

    Returns:
        strategy="reject"  : price=None，不让（首轮 / 让幅不足 1 元）
        strategy="concede" : price=本轮可给价（> 底价）
        strategy="floor"   : price=底价（托底，话术走"问主管"）
    """
    if list_price <= 0:
        raise ValueError(f"list_price 必须为正数，收到 {list_price!r}")

    ladder = ladder or DEFAULT_LADDER
    floor = round(list_price * (1 - max_discount), 2)
    rn = int(bargain_round)

    # 阶梯走完（超过最大轮次）→ 底价托底拖延，不再让价
    if rn > max(ladder):
        return _result("floor", floor, floor, rn)

    discount_rate = ladder.get(rn, 0.05)
    offer = round(list_price * (1 - discount_rate), 2)

    # 统一策略出口：offer 不低于底价、不高于原价
    if rn <= 1 or offer >= list_price:
        return _result("reject", None, floor, rn)
    if offer <= floor:
        return _result("floor", floor, floor, rn)

    # 让幅不足 1 元没意义（买家无感）→ 拒绝
    if list_price - offer < 1.0:
        return _result("reject", None, floor, rn)
    return _result("concede", offer, floor, rn)


def _result(strategy: str, price: Optional[float], floor: float, bargain_round: int) -> dict[str, Any]:
    instructions = {
        "reject": "坚定表示当前已是优惠价，可提示关注店铺活动，禁止报出任何新价格",
        "concede": f"以自然口吻给出让价 {price} 元（仅此一次），强调是对用户的专属让利，禁止报出低于该价格",
        "floor": f"表示 {price} 元已是审批底线，再低需要'问主管'拖延时间，禁止当场再降价",
    }
    return {
        "strategy": strategy,
        "price": price,
        "floor_price": floor,
        "round": int(bargain_round),
        "instruction": instructions[strategy],
    }


# =============================================================
# gift 模式（V18-P0.1 不二价人设 · daemon 接线用）
# =============================================================

# 默认赠品梯度（与 reply_scheduler._build_intent_prompt 原硬编码一致）
DEFAULT_GIFT_LADDER = {1: None, 2: "数据线", 3: "18W 充电头"}


def decide_gift_offer(bargain_round: int, gift_ladder: Optional[dict[int, str]] = None) -> dict[str, Any]:
    """不二价人设的赠品梯度决策（确定性，替代 reply_scheduler 硬编码 prompt）。

    Returns:
        strategy="reject"     : 轮次 1，不送（守价话术）
        strategy="gift"       : gift=本轮赠品（tier 递增，促单）
        strategy="final"      : 赠品拉满并促单（拍下改价马上发）
    """
    ladder = gift_ladder or DEFAULT_GIFT_LADDER
    rn = int(bargain_round)
    gift = ladder.get(rn)

    if rn <= 1:
        return {
            "strategy": "reject", "gift": None, "tier": 0, "round": rn,
            "instruction": "坚定表示当前已是优惠价，可提示关注店铺活动，禁止承诺赠品以外的让价",
        }
    if rn > max(ladder):  # 阶梯走完 → 赠品拉满促单（必须在 gift=None 判断前，末档之后无定义）
        return {
            "strategy": "final", "gift": "全部赠品", "tier": rn, "round": rn,
            "instruction": "赠品已拉满：数据线 + 18W 充电头等全送，促单（拍下改价马上发），禁止再让价",
        }
    return {
        "strategy": "gift", "gift": gift, "tier": rn, "round": rn,
        "instruction": f"价格不让，赠送{gift}作为补偿（语气自然，突出专属感），禁止降价",
    }
