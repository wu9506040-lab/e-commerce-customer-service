"""
ReplyScheduler — 随机延迟 + Agent 调 + send（V16.1.5）

职责（核心 24h 客服业务编排）：
1. 收到 debounce fire (cid, messages) → 异步线程处理
2. 30s-3min 随机 sleep（人类化，规避风控）
3. 拉 BuyerProfile + PhaseDetector 喂 context 给 LLM
4. 调智能客服 /api/chat 拿 LLM 回复
5. 调 XianyuMCPClient.send_message 真发
6. 限流（1/分钟 + 50/日）+ 消息去重 + 失败重试

调用方：
  DebounceQueue._on_timer_fire → ReplyScheduler.on_fire(cid, messages)
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import httpx

from app.services.xianyu.buyer_profile import BuyerProfile
from app.services.xianyu.escalation_detector import XianyuEscalationDetector
from app.services.xianyu.message_guard import XianyuMessageGuard
from app.services.xianyu.scenario_router import XianyuScenarioRouter
from app.services.xianyu.xianyu_prompts import XianyuPrompts

logger = logging.getLogger(__name__)


class ReplyScheduler:
    """闲鱼自动回复调度器

    设计取舍（V16.1.5）：
    - 每条 fire 独立线程（不阻塞 debounce）
    - 限流：滑动窗口（deque 存最近发送时间戳）+ 日上限（计 sent_count）
    - 去重：seen_message_ids set（最大 1000 条防内存爆）
    - 失败重试：N 次指数退避（base=1s）
    - /chat API 失败时返空（不抛；让 send_message 阶段兜底）
    """

    def __init__(
        self,
        xianyu_client,
        chat_api_url: str = "http://localhost:8000/api/chat",
        min_delay_seconds: float = 30.0,
        max_delay_seconds: float = 180.0,  # 3 分钟
        rate_limit_per_minute: int = 1,
        daily_quota: int = 50,
        max_retries: int = 2,
        chat_timeout: float = 60.0,
        db_session=None,  # V16.3 新增：用于落库 + 退款 handler
        seller_unb: str = "",  # V16.3 新增：cookie 账号 unb
    ):
        self._client = xianyu_client
        self._chat_api_url = chat_api_url
        self._min_delay = min_delay_seconds
        self._max_delay = max_delay_seconds
        self._rate_limit = rate_limit_per_minute
        self._daily_quota = daily_quota
        self._max_retries = max_retries
        self._chat_timeout = chat_timeout
        self._db = db_session
        self._seller_unb = seller_unb

        # 限流
        self._recent_send_ts: deque = deque(maxlen=rate_limit_per_minute)
        # 日计数（key=日期字符串）
        self._daily_count: dict[str, int] = {}
        # 去重
        self._seen_message_ids: set[str] = set()
        self._lock = threading.Lock()

    # =============================================================
    # 公开入口
    # =============================================================
    def on_fire(self, cid: str, messages: list[dict]) -> None:
        """DebounceQueue 调：30s 静默后触发"""
        # 异步线程处理（不阻塞 debounce 队列）
        t = threading.Thread(
            target=self._handle_fire, args=(cid, messages), daemon=True
        )
        t.start()

    def sent_count(self, day: Optional[str] = None) -> int:
        """今日已发条数（用于健康检查）"""
        day = day or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            return self._daily_count.get(day, 0)

    # =============================================================
    # 内部：编排
    # =============================================================
    def _handle_fire(self, cid: str, messages: list[dict]) -> None:
        """异步处理 fire（V16.3 集成：落库 + 退款 handler + ScenarioRouter + Escalation + MessageGuard + Prompts）"""
        # 0. V16.3 落库（先于所有其他逻辑，确保 DB 有订单数据）
        if self._db is not None and self._seller_unb:
            try:
                from app.services.xianyu.tip_parser import XianyuTipParser
                XianyuTipParser.parse_and_persist(
                    db=self._db, seller_unb=self._seller_unb, messages=messages
                )
                self._db.commit()
            except Exception as e:
                logger.warning(f"ReplyScheduler 落库失败: {e}")
                self._db.rollback()

        # 1. 消息去重
        last_msg = messages[-1] if messages else {}
        if self._is_duplicate(last_msg):
            logger.info(f"ReplyScheduler 重复消息跳过 cid={cid}")
            return
        # 2. 限流
        if not self._check_rate_limit():
            logger.warning(f"ReplyScheduler 触发限流 cid={cid}")
            return
        # 3. 解析 toid
        toid = last_msg.get("send_user_id", "")
        if not toid:
            logger.warning(f"ReplyScheduler 缺 toid 跳过 cid={cid}")
            return
        last_text = self._extract_text(last_msg)

        # 4. **V16.3 退款意图检测**（基于 DB）
        if self._db is not None and self._seller_unb and self._is_refund_intent(last_text):
            try:
                from app.services.xianyu.refund_handler import XianyuRefundHandler
                handler = XianyuRefundHandler(db=self._db, seller_unb=self._seller_unb)
                # 拉 buyer profile 拿 nick
                try:
                    profile = BuyerProfile().get_profile(unb=toid)
                    nick = profile.nick or ""
                except Exception:
                    nick = ""
                decision = handler.handle_refund_request(unb=toid, buyer_nick=nick)
                logger.info(
                    f"ReplyScheduler 退款决策 cid={cid} unb={toid} "
                    f"should_reject={decision.should_reject} "
                    f"order_state={decision.order_state}"
                )
                # 已退款：直接发拒绝话术（不调 LLM）
                # 未退款：直接发转人工话术（不调 LLM）
                self._send_with_retry(cid=cid, toid=toid, text=decision.reply_text)
                self._record_sent()
                self._mark_seen(last_msg)
                return  # 退款场景不调 LLM
            except Exception as e:
                logger.warning(f"ReplyScheduler 退款 handler 失败: {e}")

        # 5. 拉 BuyerProfile
        try:
            profile = BuyerProfile().get_profile(unb=toid)
            profile_ctx = profile.to_prompt_context()
            nick = profile.nick
            is_returning = profile.is_returning_buyer
        except Exception as e:
            logger.warning(f"ReplyScheduler BuyerProfile 失败: {e}")
            profile_ctx = ""
            nick = None
            is_returning = False

        # 6. EscalationDetector
        try:
            escalation = XianyuEscalationDetector()
            esc_result = escalation.should_escalate(unb=toid, message=last_text)
            escalation.record_message(unb=toid)
        except Exception as e:
            logger.warning(f"ReplyScheduler EscalationDetector 失败: {e}")
            esc_result = None

        if esc_result and esc_result.escalate:
            text_to_send = esc_result.reply
            logger.info(
                f"ReplyScheduler 转人工 cid={cid} reason={esc_result.reason.value if esc_result.reason else 'unknown'}"
            )
        else:
            # 7. ScenarioRouter
            try:
                router = XianyuScenarioRouter()
                scenario_result = router.route(
                    unb=toid, message=last_text, is_returning_buyer=is_returning
                )
            except Exception as e:
                logger.warning(f"ReplyScheduler ScenarioRouter 失败: {e}")
                from app.services.xianyu.scenario_router import (
                    XianyuScenarioRouter as _R,
                    Scenario as _S,
                )
                scenario_result = type("S", (), {
                    "scenario": _S.PRESALE,
                    "prompt_hint": "你是闲鱼专业卖家",
                    "should_escalate": False,
                })()

            # 8. 拼 system prompt
            try:
                prompts = XianyuPrompts()
                system_prompt = prompts.render(
                    scenario=scenario_result.scenario.value,
                    system_base="你是智能客服 Agent",
                    buyer_profile=profile_ctx,
                    nick=nick,
                )
            except Exception as e:
                logger.warning(f"ReplyScheduler XianyuPrompts 失败: {e}")
                system_prompt = f"你是智能客服 Agent\n{profile_ctx}"

            # 9. 随机延迟
            delay = self._compute_delay()
            logger.info(
                f"ReplyScheduler cid={cid} 场景={scenario_result.scenario.value} "
                f"延迟 {delay:.1f}s 后回复"
            )
            time.sleep(delay)

            # 10. 限流后 check
            if not self._check_rate_limit():
                logger.warning(f"ReplyScheduler 延迟后超额 cid={cid}")
                return

            # 11. /chat
            reply = self._call_chat_api(
                unb=toid,
                message=last_text,
                context=system_prompt,
            )
            if not reply:
                logger.warning(f"ReplyScheduler /chat 返空 cid={cid}")
                return

            # 12. MessageGuard
            try:
                guard = XianyuMessageGuard()
                allowed, text_to_send = guard.check_or_suggest(reply)
                if not allowed:
                    logger.warning(
                        f"ReplyScheduler 消息被 guard 拦截 cid={cid} "
                        f"reply={reply[:50]!r}"
                    )
            except Exception as e:
                logger.warning(f"ReplyScheduler MessageGuard 失败: {e}")
                text_to_send = reply

        # 13. send_message 真发
        result = self._send_with_retry(cid=cid, toid=toid, text=text_to_send)
        if result.get("ok") or result.get("message_id"):
            self._record_sent()
            self._mark_seen(last_msg)
            logger.info(
                f"ReplyScheduler cid={cid} 已发 message_id={result.get('message_id')}"
            )
        else:
            logger.error(
                f"ReplyScheduler cid={cid} 发送失败: {result.get('error')}"
            )

    @staticmethod
    def _is_refund_intent(text: str) -> bool:
        """检测退款意图（基于关键词）"""
        if not text:
            return False
        keywords = [
            "退款", "退钱", "退一下", "退回来", "我要退",
            "申请退款", "怎么退", "能退吗", "用不了",
        ]
        return any(kw in text for kw in keywords)

    def _build_context(self, messages: list[dict]) -> str:
        """拼 LLM context（保留兼容：仅 message history）"""
        lines = []
        last = messages[-1] if messages else {}
        last_text = self._extract_text(last)
        lines.append(f"【闲鱼买家最近消息】{last_text}")
        return "\n".join(lines)

    def _extract_text(self, event: dict) -> str:
        """从事件里提 message text（兼容 goofish 多形状）"""
        msg = event.get("message") or {}
        if not isinstance(msg, dict):
            return ""
        text = msg.get("text") or {}
        if isinstance(text, dict):
            return text.get("text", "")
        return str(text) if text else ""

    # =============================================================
    # 内部：随机延迟
    # =============================================================
    def _compute_delay(self) -> float:
        """区间内均匀随机"""
        return random.uniform(self._min_delay, self._max_delay)

    # =============================================================
    # 内部：限流
    # =============================================================
    def _check_rate_limit(self) -> bool:
        """滑动窗口 + 日上限"""
        now = time.time()
        with self._lock:
            # 清掉 60s 外的
            while self._recent_send_ts and (now - self._recent_send_ts[0]) > 60:
                self._recent_send_ts.popleft()
            if len(self._recent_send_ts) >= self._rate_limit:
                return False
            # 日上限
            today = datetime.now().strftime("%Y-%m-%d")
            if self._daily_count.get(today, 0) >= self._daily_quota:
                return False
        return True

    def _record_sent(self) -> None:
        """记录一次发送（写滑动窗口 + 日计数）"""
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            self._recent_send_ts.append(now)
            self._daily_count[today] = self._daily_count.get(today, 0) + 1
            # 清理旧日计数（只留今天）
            self._daily_count = {d: c for d, c in self._daily_count.items() if d == today}

    # =============================================================
    # 内部：去重
    # =============================================================
    def _is_duplicate(self, event: dict) -> bool:
        msg_id = event.get("message_id", "")
        if not msg_id:
            return False  # 无 id 不去重（保守）
        with self._lock:
            if msg_id in self._seen_message_ids:
                return True
        return False

    def _mark_seen(self, event: dict) -> None:
        msg_id = event.get("message_id", "")
        if not msg_id:
            return
        with self._lock:
            self._seen_message_ids.add(msg_id)
            # 防止内存爆：超过 5000 清掉最早的 1000
            if len(self._seen_message_ids) > 5000:
                # 简单清法：清一半（无序，但够用）
                self._seen_message_ids = set(
                    list(self._seen_message_ids)[2500:]
                )

    # =============================================================
    # 内部：智能客服 /chat 调用（SSE 解析）
    # =============================================================
    def _call_chat_api(self, unb: str, message: str, context: str) -> str:
        """调 /api/chat SSE 流，提取 answer

        V16.2 升级：context 直接作 system prompt 喂 LLM（含场景 + 买家画像）
        """
        try:
            payload = {
                "query": message,
                "user_id": None,
                "channel": "xianyu",
                "buyer_unb": unb,
                "context_block": context,
                "system_prompt": context,  # 兼容模式：context 既是 context_block 也是 system
            }
            resp = httpx.post(
                self._chat_api_url,
                json=payload,
                timeout=self._chat_timeout,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"/chat 返 {resp.status_code}: {resp.text[:200]}"
                )
                return ""
            # 解析 SSE（按 'data: {...}\n\n' 分块）
            full_text = ""
            for chunk in resp.text.split("\n\n"):
                chunk = chunk.strip()
                if not chunk.startswith("data:"):
                    continue
                payload_str = chunk[5:].strip()
                if payload_str == "[DONE]":
                    break
                try:
                    import json
                    obj = json.loads(payload_str)
                    # 智能客服 SSE 事件类型兼容：
                    # - 标准 V1: {event: "token", text: "..."} / {event: "done", answer: "..."}
                    # - 简化 V0: 直接 {answer: "..."}（一块到位）
                    if obj.get("event") == "token" or obj.get("type") == "token":
                        full_text += obj.get("text", "") or obj.get("data", "")
                    elif obj.get("event") == "done" or obj.get("type") == "done":
                        ans = obj.get("answer", "") or obj.get("data", {}).get("answer", "")
                        if ans:
                            full_text = ans
                    elif "answer" in obj and obj["answer"]:
                        # 简化格式：直接包含 answer 字段
                        full_text = obj["answer"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
            return full_text.strip()
        except Exception as e:
            logger.warning(
                f"/chat 调用失败: {type(e).__name__}: {e}"
            )
            return ""

    # =============================================================
    # 内部：send_message + 失败重试
    # =============================================================
    def _send_with_retry(self, cid: str, toid: str, text: str) -> dict:
        """send_message + 指数退避重试"""
        last_result = {"error": "no_attempt"}
        for attempt in range(self._max_retries + 1):
            result = self._client.send_message(
                cid=cid, toid=toid, text=text
            )
            if result.get("ok") or result.get("message_id"):
                return result
            last_result = result
            if attempt < self._max_retries:
                wait = 1.0 * (2 ** attempt)  # 1s, 2s, 4s
                logger.warning(
                    f"send_message 失败 (attempt {attempt+1}/{self._max_retries+1}) "
                    f"等 {wait}s: {result.get('error')}"
                )
                time.sleep(wait)
        return last_result


__all__ = ["ReplyScheduler"]