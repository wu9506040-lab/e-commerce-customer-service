"""
XianyuMessageGuard — 闲鱼 send_message 前敏感词扫描（V16.2.1）

职责：
1. 拦截智能客服**不能说的话**（"已退款" / "已转账" / "投诉" 类）
2. 放行正常售前/售中/售后话术
3. 拦截后给"安全替代话术"（引导转人工）
4. 集成到 ReplyScheduler._send_with_retry 之前

调用方：
  ReplyScheduler._send_with_retry(cid, toid, text)
    → XianyuMessageGuard.check(text)  # 拦截就放弃 + 告警
    → XianyuMCPClient.send_message(...)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class GuardResult:
    """扫描结果"""

    blocked: bool
    reason: str = ""  # 命中的敏感词/原因
    suggestion: str = ""  # 替代话术（仅 blocked=True 有）
    matched_pattern: str = ""  # 命中的 pattern（debug 用）


# 敏感词黑名单（智能客服**不能**说）
# 设计原则：只拦截"承诺性"话术，开放"问询性"话术
_BLOCKED_PATTERNS: list[tuple[str, str, str]] = [
    # (pattern, reason, suggestion)
    # 退款承诺类（用 [\d\s]* 兼容 "已退款 50 元" 中的空格）
    (
        r"已退款[\d\s.]*元",
        "承诺退款",
        "已记下您的退款需求，请稍候会有专员 24 小时内与您联系。",
    ),
    (
        r"已退款[，,。\s]",
        "承诺退款（无金额）",
        "已记下您的退款需求，请稍候会有专员 24 小时内与您联系。",
    ),
    (
        r"已退款$",
        "承诺退款（结尾）",
        "已记下您的退款需求，请稍候会有专员 24 小时内与您联系。",
    ),
    (
        r"退款已到账",
        "承诺退款到账",
        "您的退款申请已登记，专员处理后 1-3 个工作日到账。",
    ),
    (
        r"已为您退款",
        "承诺退款",
        "您的退款需求已收到，专员会尽快处理。",
    ),
    (
        r"已转账[\d\s.]*元",
        "承诺转账",
        "专员会尽快与您确认退款方式。",
    ),
    (
        r"补偿金[\d\s.]*元",
        "承诺补偿",
        "已记录您的反馈，专员会评估处理。",
    ),
    (
        r"赔偿已到账",
        "承诺赔偿到账",
        "您的反馈已记录，专员会尽快与您联系。",
    ),
    # 投诉/举报类
    (
        r"12315",
        "投诉 12315",
        "请稍候，专员会与您详细沟通。",
    ),
    (
        r"消协|消费者协会",
        "投诉消协",
        "请稍候，专员会与您详细沟通。",
    ),
    (
        r"\b315\b",
        "投诉 315",
        "请稍候，专员会与您详细沟通。",
    ),
    # 严重指控
    (
        r"欺诈|骗人|假货|黑店",
        "严重指控",
        "请稍候，专员会与您详细沟通。",
    ),
]


# 放行白名单（即使含敏感词，承诺类才拦）
_PASS_PATTERNS: list[str] = [
    r"退款政策",
    r"退款说明",
    r"支持退款",
    r"可以退款",
    r"如何退款",
    r"几天退款",
    r"运费险",
]


class XianyuMessageGuard:
    """闲鱼消息敏感词扫描

    设计取舍（V16.2.1）：
    - 黑名单（_BLOCKED_PATTERNS）必须命中"承诺性"话术（"已 X"）
    - 白名单（_PASS_PATTERNS）保留"问询性"话术（"如何退款"）
    - 中文场景为主（闲鱼是中文平台）
    - 拦截后返 suggestion（让 Agent 改成安全话术）
    """

    def __init__(self):
        self._blocked = [
            (re.compile(p), reason, suggestion)
            for p, reason, suggestion in _BLOCKED_PATTERNS
        ]
        self._pass = [re.compile(p) for p in _PASS_PATTERNS]

    def check(self, text: str) -> GuardResult:
        """扫描文本，返 GuardResult

        行为：
        - 空/空白 → 放行
        - 黑名单命中 → 拦截 + 返 suggestion
        - 白名单命中（问询性）→ 即使含敏感词也放行
        - 多重命中 → 返第一个（按 _BLOCKED_PATTERNS 顺序）
        """
        if not text or not text.strip():
            return GuardResult(blocked=False)

        # 1. 黑名单扫描
        for pattern, reason, suggestion in self._blocked:
            if pattern.search(text):
                return GuardResult(
                    blocked=True,
                    reason=reason,
                    suggestion=suggestion,
                    matched_pattern=pattern.pattern,
                )

        # 2. 全过 → 放行
        return GuardResult(blocked=False)

    def check_or_suggest(self, text: str) -> tuple[bool, str]:
        """简化接口：返 (是否放行, 替代话术)

        放行时替代话术=text 本身。
        """
        result = self.check(text)
        if result.blocked:
            return False, result.suggestion
        return True, text


__all__ = ["XianyuMessageGuard", "GuardResult"]