"""
XianyuScenarioRouter — 闲鱼售前/售中/售后场景路由（V16.2.3）

职责：
1. 根据消息内容 + 买家画像 → 路由到售前/售中/售后
2. 给 LLM 提供场景化 hint（差异化话术）
3. 退款类单独标记为 ESCALATE（不处理，转人工）

调用方：
  ReplyScheduler.on_fire → XianyuScenarioRouter.route(unb, message, is_returning_buyer)
    → ScenarioResult（scenario + prompt_hint + should_escalate）
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Scenario(str, Enum):
    """闲鱼场景分类"""

    PRESALE = "presale"  # 售前（主战场）
    IN_SALE = "in_sale"  # 售中
    AFTER_SALE = "after_sale"  # 售后
    ESCALATE = "escalate"  # 转人工（退款/投诉等）

    @property
    def chinese(self) -> str:
        return {
            Scenario.PRESALE: "售前",
            Scenario.IN_SALE: "售中",
            Scenario.AFTER_SALE: "售后",
            Scenario.ESCALATE: "转人工",
        }[self]


@dataclass
class ScenarioResult:
    """场景路由结果"""

    scenario: Scenario
    prompt_hint: str  # 喂给 LLM 的场景化指令
    should_escalate: bool = False  # 是否需要转人工（即使路由到场景也提醒）
    confidence: float = 1.0  # 路由置信度（未来可量化）


# 退款关键词（**优先**于场景分类，命中即转人工）
_REFUND_PATTERNS = [
    r"退款", r"退钱", r"退一下", r"退回来", r"我要退",
]

# 售中关键词
_IN_SALE_PATTERNS = [
    r"发货", r"快递", r"物流", r"到哪了",
    r"改地址", r"改收货", r"换地址", r"修改地址",
    r"催促", r"催发", r"还没发", r"什么时候发",
    r"还没收到", r"已签收", r"快递单号", r"运单",
]

# 售后关键词（不含退款）
_AFTER_SALE_PATTERNS = [
    r"换货", r"换码", r"换尺码", r"换颜色",
    r"质量有问题", r"质量差", r"质量不行",
    r"有瑕", r"坏了", r"坏掉",
    r"好评", r"五星", r"评价", r"满意",
    r"补发", r"少件", r"漏发",
    r"使用", r"怎么用", r"说明书", r"保修",
]

# 售前关键词（**保留场景**——议价/商品咨询/材质/质量怎样）
_PRESALE_PATTERNS = [
    r"在吗", r"便宜", r"讲价", r"议价", r"价格",
    r"包邮", r"邮费", r"运费",
    r"材质", r"材料", r"是什么", r"好用吗",
    r"还有货", r"库存", r"颜色", r"尺码", r"大小",
    r"新品", r"二手", r"新旧", r"瑕疵", r"缺陷",
    r"收到了",  # 售前也问"收到货怎么用"
    r"质量怎么", r"质量如何", r"质量怎样",  # 售前问
    r"质量怎么样",
]


# 场景化 hint（给 LLM 注入 system prompt）
_SCENARIO_HINTS = {
    Scenario.PRESALE: """
【场景：售前咨询】
你的角色是专业卖家，不是套话客服。回复要：
1. 用买家昵称（如果有）
2. 给出真实数据（库存/材质/批次），不要"亲，是的呢"那种套话
3. 议价策略：看老客/新客，新客首次小让或送配件，老客可以小让
4. 同类对比要诚实，敢说"我家不包邮"+"主动说瑕疵"
5. 议价失败不套路，"这价格已经是底了"
""",
    Scenario.IN_SALE: """
【场景：售中跟进】
买家已经拍下，关注物流/地址/催促：
1. 发货时间承诺（"今天 18:00 前拍今天发"）
2. 物流查询引导（"单号 XXX，预计明天到"）
3. 改地址：未发货可直接调 modify_address（需 confirmed=true）；已发货转人工
4. 催促 2 次以上：转人工
""",
    Scenario.AFTER_SALE: """
【场景：售后处理】（不含退款）
1. 换货/换码：直接调 modify_item_spec（需 confirmed=true）
2. 质量问题：主动提供 7 天换新 + 运费承担
3. 评价引导：买家说"满意"时主动邀评
4. **不要谈退款**（智能客服无权承诺退款，引导"请到闲鱼 App 申请或人工客服"）
""",
    Scenario.ESCALATE: """
【场景：转人工处理】
本次回复是过渡话术，承诺转专员处理：
1. 不承诺具体退款金额或时间
2. 不解释具体原因
3. 统一话术："已转专员，请稍候"
""",
}


class XianyuScenarioRouter:
    """闲鱼场景路由

    设计取舍（V16.2.3）：
    - 关键词匹配为主（不调 LLM，0 成本 + 0 延迟）
    - 退款优先于场景分类（命中即 ESCALATE）
    - 老客影响路由：售前/售中边界用 is_returning_buyer 辅助
    - 空/未知消息 → 默认售前（Agent 自由发挥）
    """

    def __init__(self):
        self._refund = [re.compile(p) for p in _REFUND_PATTERNS]
        self._in_sale = [re.compile(p) for p in _IN_SALE_PATTERNS]
        self._after_sale = [re.compile(p) for p in _AFTER_SALE_PATTERNS]
        self._presale = [re.compile(p) for p in _PRESALE_PATTERNS]

    def route(
        self, unb: str, message: str, is_returning_buyer: bool = False
    ) -> ScenarioResult:
        """路由到场景 + 给 LLM hint"""
        msg = (message or "").strip()
        if not msg:
            return ScenarioResult(
                scenario=Scenario.PRESALE,
                prompt_hint=_SCENARIO_HINTS[Scenario.PRESALE],
            )

        # 1. 退款优先（最高优先级）
        if self._match_any(self._refund, msg):
            return ScenarioResult(
                scenario=Scenario.ESCALATE,
                prompt_hint=_SCENARIO_HINTS[Scenario.ESCALATE],
                should_escalate=True,
            )

        # 2. 售中
        if self._match_any(self._in_sale, msg):
            return ScenarioResult(
                scenario=Scenario.IN_SALE,
                prompt_hint=_SCENARIO_HINTS[Scenario.IN_SALE],
            )

        # 3. 售后
        if self._match_any(self._after_sale, msg):
            return ScenarioResult(
                scenario=Scenario.AFTER_SALE,
                prompt_hint=_SCENARIO_HINTS[Scenario.AFTER_SALE],
            )

        # 4. 售前（默认）
        return ScenarioResult(
            scenario=Scenario.PRESALE,
            prompt_hint=_SCENARIO_HINTS[Scenario.PRESALE],
        )

    def _match_any(self, patterns: list[re.Pattern], text: str) -> bool:
        return any(p.search(text) for p in patterns)


__all__ = ["XianyuScenarioRouter", "Scenario", "ScenarioResult"]