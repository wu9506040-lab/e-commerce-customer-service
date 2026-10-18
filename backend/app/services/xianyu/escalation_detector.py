"""
XianyuEscalationDetector — 闲鱼转人工规则（V16.2.2）

职责：
1. 检测触发转人工的关键词/规则
2. 累计买家行为计数（"不满意"次数 / 24h 消息数）
3. 提供转人工后的话术模板

转人工触发优先级（从高到低）：
1. 严重指控（欺诈/假货/黑店）→ 立刻转
3. 投诉/举报/12315/消协/315 → 立刻转
3. 退款（所有变体）→ 立刻转（V16.2 不做退款场景）
4. 用户明确要求（转人工/找老板）→ 立刻转
5. 情绪对抗（连续 2 次不满意）→ 转
6. 异常行为（24h ≥5 次）→ 转

调用方：
  ReplyScheduler.on_fire → XianyuEscalationDetector.should_escalate(unb, message)
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class EscalationReason(str, Enum):
    """转人工原因"""

    SEVERE_ACCUSATION = "severe_accusation"  # 严重指控
    COMPLAINT = "complaint"  # 投诉/举报
    REFUND = "refund"  # 退款
    USER_REQUEST = "user_request"  # 用户明确要求
    EMOTION = "emotion"  # 情绪对抗
    ABNORMAL_BEHAVIOR = "abnormal_behavior"  # 异常行为

    @property
    def chinese(self) -> str:
        return {
            EscalationReason.SEVERE_ACCUSATION: "严重指控",
            EscalationReason.COMPLAINT: "投诉/举报",
            EscalationReason.REFUND: "退款申请",
            EscalationReason.USER_REQUEST: "用户要求",
            EscalationReason.EMOTION: "情绪对抗",
            EscalationReason.ABNORMAL_BEHAVIOR: "异常行为",
        }[self]


# 触发关键词（按优先级排，**先命中先转**）
_TRIGGER_RULES: list[tuple[EscalationReason, list[str]]] = [
    # 1. 严重指控（最高优先）
    (EscalationReason.SEVERE_ACCUSATION, [
        r"欺诈", r"骗人", r"假货", r"黑店",
    ]),
    # 2. 投诉/举报
    (EscalationReason.COMPLAINT, [
        r"投诉", r"举报", r"12315", r"消协", r"消费者协会", r"\b315\b",
    ]),
    # 3. 退款（V16.2 全部转人工）
    (EscalationReason.REFUND, [
        r"退款", r"退钱", r"退一下", r"退回来", r"我要退",
    ]),
    # 4. 用户明确要求
    (EscalationReason.USER_REQUEST, [
        r"转人工", r"找老板", r"叫老板", r"你们老板", r"人工客服", r"真人",
    ]),
]


# 转人工后的话术模板（按 reason 给不同回复）
_REPLY_TEMPLATES: dict[EscalationReason, str] = {
    EscalationReason.SEVERE_ACCUSATION:
        "您的反馈我们非常重视，已转给专员处理，请稍候。",
    EscalationReason.COMPLAINT:
        "非常抱歉给您带来困扰，已为您转接专员，请稍候。",
    EscalationReason.REFUND:
        "退款申请已收到，已为您转接专员，24 小时内会有工作人员与您联系。",
    EscalationReason.USER_REQUEST:
        "好的，已为您转接专员，请稍候片刻。",
    EscalationReason.EMOTION:
        "抱歉没能解决您的问题，已为您转接专员，请稍候。",
    EscalationReason.ABNORMAL_BEHAVIOR:
        "已为您转接专员，请稍候。",
}


@dataclass
class EscalationResult:
    """转人工判定结果"""

    escalate: bool
    reason: Optional[EscalationReason] = None
    reply: str = ""  # 给买家的话术（仅 escalate=True 有内容）


class XianyuEscalationDetector:
    """闲鱼转人工检测器

    设计取舍（V16.2.2）：
    - 状态最小化（不持久化；重启清零）
    - 同 unb 行为计数（deque 存时间戳）
    - 关键词 regex 编译一次（init 时）
    """

    # 异常行为阈值
    # V18 fix（2026-10-17）：5条/24h 会把正常买家误伤——实测一次售前咨询
    # （在吗+测试26+这个容量多大，加上上午联调）就累计超 5 条，之后所有消息
    # 都被 转人工(abnormal_behavior) 短路，/chat 根本不进来。
    # 改为 30条/1h：只拦刷屏/轰炸，正常聊天与测试不再误触发。
    ABNORMAL_BEHAVIOR_THRESHOLD = 30
    EMOTION_THRESHOLD = 2  # 连续 2 次"不满意"触发
    ABNORMAL_WINDOW_SECONDS = 3600  # 1h（原 24h 过长，一次咨询就爆）

    def __init__(self):
        self._trigger_patterns: list[tuple[EscalationReason, list[re.Pattern]]] = [
            (reason, [re.compile(p) for p in patterns])
            for reason, patterns in _TRIGGER_RULES
        ]
        # 行为计数
        self._message_log: dict[str, deque] = {}  # unb → 时间戳 deque
        self._unsatisfied_count: dict[str, int] = {}  # unb → 连续不满意次数

    def should_escalate(self, unb: str, message: str) -> EscalationResult:
        """判定是否转人工 + 给出话术"""
        if not message or not message.strip():
            return EscalationResult(escalate=False)

        # 1. 关键词规则（按优先级匹配）
        for reason, patterns in self._trigger_patterns:
            for pattern in patterns:
                if pattern.search(message):
                    return EscalationResult(
                        escalate=True,
                        reason=reason,
                        reply=_REPLY_TEMPLATES[reason],
                    )

        # 2. 情绪对抗（连续 2 次不满意）
        if self._is_unsatisfied(message):
            count = self._unsatisfied_count.get(unb, 0) + 1
            self._unsatisfied_count[unb] = count
            if count >= self.EMOTION_THRESHOLD:
                # 触发后清零（避免持续计数）
                self._unsatisfied_count[unb] = 0
                return EscalationResult(
                    escalate=True,
                    reason=EscalationReason.EMOTION,
                    reply=_REPLY_TEMPLATES[EscalationReason.EMOTION],
                )
            return EscalationResult(escalate=False)
        else:
            # 不满意之外的话 → 清零
            self._unsatisfied_count[unb] = 0

        # 3. 异常行为（24h ≥ N 次消息）
        if self._is_abnormal_behavior(unb):
            return EscalationResult(
                escalate=True,
                reason=EscalationReason.ABNORMAL_BEHAVIOR,
                reply=_REPLY_TEMPLATES[EscalationReason.ABNORMAL_BEHAVIOR],
            )

        return EscalationResult(escalate=False)

    def record_message(self, unb: str) -> None:
        """记录一次消息（用于异常行为计数）"""
        log = self._message_log.setdefault(unb, deque())
        log.append(time.time())
        # 清理 24h 外
        cutoff = time.time() - self.ABNORMAL_WINDOW_SECONDS
        while log and log[0] < cutoff:
            log.popleft()

    def record_unsatisfied(self, unb: str) -> None:
        """外部记录一次不满意（测试用 / 调度器外调）"""
        self._unsatisfied_count[unb] = self._unsatisfied_count.get(unb, 0) + 1

    def get_escalation_reply(self, reason: EscalationReason) -> str:
        """取转人工话术"""
        return _REPLY_TEMPLATES.get(reason, "已转专员，请稍候。")

    # =============================================================
    # 内部
    # =============================================================
    def _is_unsatisfied(self, message: str) -> bool:
        """是否含'不满意'类话术"""
        return any(
            kw in message
            for kw in ["不满意", "不行", "没用", "没解决", "答非所问", "听不懂"]
        )

    def _is_abnormal_behavior(self, unb: str) -> bool:
        """同 unb 24h 内消息数 ≥ 阈值"""
        log = self._message_log.get(unb)
        if not log:
            return False
        # 清理 24h 外
        cutoff = time.time() - self.ABNORMAL_WINDOW_SECONDS
        while log and log[0] < cutoff:
            log.popleft()
        return len(log) >= self.ABNORMAL_BEHAVIOR_THRESHOLD


__all__ = [
    "XianyuEscalationDetector",
    "EscalationReason",
    "EscalationResult",
]