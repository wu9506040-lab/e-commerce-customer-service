"""
ReplyScheduler — V16.3.1 首次 10-15s / 后续 0-3s + Agent 调 + send

职责（核心 24h 客服业务编排）：
1. 收到 debounce fire (cid, messages) → 异步线程处理
2. 首次延迟 10-15s（首次可慢点查卖家信息，<30s 总时间）
   后续延迟 0-3s（已有 BuyerProfile 缓存，<10s 总时间）
3. 拉 BuyerProfile + PhaseDetector 喂 context 给 LLM
4. 调智能客服 /api/chat 拿 LLM 回复
5. 调 XianyuMCPClient.send_message 真发
6. 限流（1/分钟 + 50/日）+ 消息去重 + 失败重试

调用方：
  DebounceQueue._on_timer_fire → ReplyScheduler.on_fire(cid, messages)
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import httpx
import json as _json  # V16.3 SSE 解析需要

# V16.5: XianyuPrompts 改 lazy import（在 _get_prompts 内）
# 原顶部 import 会导致 ReplyScheduler 实例化时立即 load 3 个 yaml,延迟启动
# 改为 lazy 后,YAML 只在第一次 _handle_fire 触发时 load 一次,后续 fire 复用实例

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

    # V16.6: 限流响应里的"再等 X.Xs"模式（用于解析剩余秒数）
    # 实际响应：'限流：bucket=message.write 每 60s 上限 1，再等 53.1s'
    _RATE_LIMIT_PATTERN = re.compile(r"再等\s*([\d.]+)\s*s", re.IGNORECASE)
    # V16.8 P1-B: 限流场景独立重试配额（实际"无上限"，daemon 不死就一定能发出）
    # 旧：5 次后放弃 → 用户消息丢失
    # 新：99999 次（实际等同无限，保留计数器只为日志/监控可观测）
    _RATE_LIMIT_MAX_RETRIES = 99999
    # V16.8 P1-B: 非限流错误指数退避（5s/10s/20s/40s/60s cap）
    _NON_RATE_LIMIT_BACKOFF_BASE_SEC = 5.0
    _NON_RATE_LIMIT_BACKOFF_MAX_SEC = 60.0
    # V16.7: 多消息合并 prompt 模板（用户连发时拼综合 query）
    _MULTI_MSG_PROMPT_HEADER = "用户连续发了以下几条消息："
    _MULTI_MSG_PROMPT_FOOTER = "请你综合回答这几个问题，一条回复里覆盖所有问题。"

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
        # V16.8 P1-D: 首次 vs 后续统一延迟区间
        # 旧(V16.3.1)：首次 10-15s（"显得像真人"）—— 反功能：买家期望 < 3s 真人响应
        # 新：首次/后续都 0-3s — 真人客服 IM 响应节奏
        self._first_min_delay = float(os.environ.get("XIANYU_FIRST_MIN_DELAY", "0"))
        self._first_max_delay = float(os.environ.get("XIANYU_FIRST_MAX_DELAY", "3"))
        self._next_min_delay = float(os.environ.get("XIANYU_NEXT_MIN_DELAY", "0"))
        self._next_max_delay = float(os.environ.get("XIANYU_NEXT_MAX_DELAY", "3"))
        # 保留旧字段（兼容）
        self._min_delay = min_delay_seconds
        self._max_delay = max_delay_seconds
        self._rate_limit = rate_limit_per_minute
        self._daily_quota = daily_quota
        self._max_retries = max_retries
        self._chat_timeout = chat_timeout
        self._db = db_session
        self._seller_unb = seller_unb

        # V16.5: lazy 模块单例 — 避免每条消息 new 一次（之前 _handle_fire 调 4 次
        #   BuyerProfile / EscalationDetector / ScenarioRouter / XianyuPrompts 每次
        #   都 new → 累计 3-5 秒/条 主要性能瓶颈之一）
        self._buyer_profile_singleton: object | None = None
        self._escalation_detector_singleton: object | None = None
        self._scenario_router_singleton: object | None = None
        self._prompts_singleton: object | None = None

        # V16.3.1: 跟踪每个 unb 的首次/后续 fire 状态
        self._unb_first_fire_done: set[str] = set()
        # V16.3.1: 消息去重（保留旧逻辑）
        self._seen_message_ids: set[str] = set()
        # 限流（保留）
        self._recent_send_ts: deque = deque(maxlen=rate_limit_per_minute)
        self._daily_count: dict[str, int] = {}
        self._lock = threading.Lock()

    # =============================================================
    # 公开入口
    # =============================================================
    def on_fire(self, cid: str, messages: list[dict]) -> None:
        """DebounceQueue 调：V16.3.1 首次 5s/后续 3s 静默后触发"""
        msg_events = [m for m in (messages or []) if m.get("event") == "message"]
        if not msg_events:
            logger.info(f"on_fire 0 message 事件 cid={cid}（原 {len(messages or [])} 条）")
            return
        t = threading.Thread(target=self._handle_fire, args=(cid, msg_events), daemon=True)
        t.start()

    # =============================================================
    # V16.5: Lazy 模块单例（每个 ReplyScheduler 实例内部，跨 fire 复用）
    # =============================================================
    def _get_buyer_profile(self):
        """BuyerProfile 单例(内部缓存) — 避免每条消息 new"""
        if self._buyer_profile_singleton is None:
            from app.services.xianyu.buyer_profile import BuyerProfile
            self._buyer_profile_singleton = BuyerProfile()
        return self._buyer_profile_singleton

    def _get_escalation_detector(self):
        if self._escalation_detector_singleton is None:
            from app.services.xianyu.escalation_detector import XianyuEscalationDetector
            self._escalation_detector_singleton = XianyuEscalationDetector()
        return self._escalation_detector_singleton

    def _get_scenario_router(self):
        if self._scenario_router_singleton is None:
            from app.services.xianyu.scenario_router import XianyuScenarioRouter
            self._scenario_router_singleton = XianyuScenarioRouter()
        return self._scenario_router_singleton

    def _get_prompts(self):
        if self._prompts_singleton is None:
            from app.services.xianyu.xianyu_prompts import XianyuPrompts
            self._prompts_singleton = XianyuPrompts()
        return self._prompts_singleton

    def sent_count(self, day: Optional[str] = None) -> int:
        """今日已发条数（用于健康检查）"""
        day = day or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            return self._daily_count.get(day, 0)

    # =============================================================
    # 内部：编排
    # =============================================================
    def _build_query(self, messages: list[dict]) -> tuple[str, bool]:
        """V16.7: 把多条消息拼成单条 query

        Returns:
            (query, is_multi)
            - query: 单条消息就是原文；多条拼成综合 prompt
            - is_multi: 是否为合并后的多消息（True 时下游 hint LLM 综合回答）

        合并格式（用户指定）：
            用户连续发了以下几条消息：
            1. 在吗
            2. 这个充电宝多大容量
            3. 能便宜点吗
            请你综合回答这几个问题，一条回复里覆盖所有问题。
        """
        texts = [self._extract_text(m) for m in messages]
        # 去空（保留顺序）
        texts = [t.strip() for t in texts if t and t.strip()]
        if not texts:
            return "", False
        if len(texts) == 1:
            return texts[0], False
        # 多条：拼成综合 prompt
        lines = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        query = (
            f"{self._MULTI_MSG_PROMPT_HEADER}\n{lines}\n"
            f"{self._MULTI_MSG_PROMPT_FOOTER}"
        )
        return query, True

    def _is_refund_intent_any(self, messages: list[dict]) -> bool:
        """V16.7: 任一消息含退款关键词 → 命中（合并后批量判断）"""
        for m in messages:
            if self._is_refund_intent(self._extract_text(m)):
                return True
        return False

    def _is_duplicate_any(self, messages: list[dict]) -> bool:
        """V16.7: 任一消息已 seen → 整批视为重复（防止部分已处理）"""
        for m in messages:
            if self._is_duplicate(m):
                return True
        return False

    def _mark_seen_any(self, messages: list[dict]) -> None:
        """V16.7: 标记所有消息为 seen"""
        for m in messages:
            self._mark_seen(m)

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

        # V16.7: 多消息合并 — 拼成单条 query + 任一已见则跳过
        last_msg = messages[-1] if messages else {}
        if self._is_duplicate_any(messages):
            logger.info(f"ReplyScheduler 重复消息跳过 cid={cid} batch={len(messages)}")
            return
        # 2. 限流
        if not self._check_rate_limit():
            logger.warning(f"ReplyScheduler 触发限流 cid={cid}")
            # V16.5 全链路日志：L6 限流详情
            try:
                from app.core.runtime_log import log_event
                recent_count = len(self._recent_send_ts)
                oldest_ts = self._recent_send_ts[0] if self._recent_send_ts else 0
                oldest_age_sec = time.time() - oldest_ts if oldest_ts else 0
                unb = last_msg.get("send_user_id", "-") if last_msg else "-"
                if log_event:
                    log_event("RATE_LIMIT", "WARNING", cid, str(unb),
                              f"rate limit rejected threshold={self._rate_limit}/min",
                              reason="per_minute_exceeded",
                              threshold=self._rate_limit,
                              recent_count=recent_count,
                              oldest_send_age_sec=round(oldest_age_sec, 1),
                              daily_quota=self._daily_quota,
                              daily_count=self._daily_count.get(datetime.now().strftime("%Y-%m-%d"), 0))
            except Exception:
                pass
            return
        # 3. 解析 toid
        toid = last_msg.get("send_user_id", "")
        if not toid:
            logger.warning(f"ReplyScheduler 缺 toid 跳过 cid={cid}")
            return
        # V16.7: 多消息合并 query
        last_text, is_multi = self._build_query(messages)
        if not last_text:
            logger.warning(f"ReplyScheduler 拼 query 为空 cid={cid}")
            return
        # V16.7: 打印合并后的 query（确认 V16.7 合并行为生效）
        logger.info(
            f"ReplyScheduler cid={cid} query_merged is_multi={is_multi} "
            f"batch={len(messages)} text=[{last_text[:200]}]"
        )
        try:
            from app.core.runtime_log import log_event
            log_event("QUERY_MERGED", "INFO", cid, str(toid),
                      "merged multi messages into one query for LLM",
                      is_multi=is_multi, batch_count=len(messages),
                      query_preview=last_text[:300])
        except Exception:
            pass

        # 4. **V16.3 退款意图检测**（基于 DB）
        if self._db is not None and self._seller_unb and self._is_refund_intent_any(messages):
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
                self._send_with_retry(cid=cid, toid=toid, text=decision.reply_text)
                self._record_sent()
                self._mark_seen_any(messages)  # V16.7: 合并标记所有
                return  # 退款场景不调 LLM
            except Exception as e:
                logger.warning(f"ReplyScheduler 退款 handler 失败: {e}")

        # 5. 拉 BuyerProfile（V16.5 lazy 单例 — 跨 fire 复用）
        try:
            profile = self._get_buyer_profile().get_profile(unb=toid)
            profile_ctx = profile.to_prompt_context()
            nick = profile.nick
            is_returning = profile.is_returning_buyer
        except Exception as e:
            logger.warning(f"ReplyScheduler BuyerProfile 失败: {e}")
            profile_ctx = ""
            nick = None
            is_returning = False

        # 6. EscalationDetector（V16.5 lazy 单例）
        try:
            escalation = self._get_escalation_detector()
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
            # 7. ScenarioRouter（V16.5 lazy 单例）
            try:
                scenario_result = self._get_scenario_router().route(
                    unb=toid, message=last_text, is_returning_buyer=is_returning
                )
            except Exception as e:
                logger.warning(f"ReplyScheduler ScenarioRouter 失败: {e}")
                from app.services.xianyu.scenario_router import Scenario as _S
                scenario_result = type("S", (), {
                    "scenario": _S.PRESALE,
                    "prompt_hint": "你是闲鱼专业卖家",
                    "should_escalate": False,
                })()

            # 8. 拼 system prompt（V16.5 lazy 单例 — 避免每次 on_fire 重读 3 个 yaml）
            try:
                prompts = self._get_prompts()
                system_prompt = prompts.render(
                    scenario=scenario_result.scenario.value,
                    system_base="你是智能客服 Agent",
                    buyer_profile=profile_ctx,
                    nick=nick,
                )
            except Exception as e:
                logger.warning(f"ReplyScheduler XianyuPrompts 失败: {e}")
                system_prompt = f"你是智能客服 Agent\n{profile_ctx}"

            # 9. 随机延迟（V16.3.1：首次 10-15s / 后续 0-3s）
            delay = self._compute_delay()
            logger.info(
                f"ReplyScheduler cid={cid} 场景={scenario_result.scenario.value} "
                f"延迟 {delay:.1f}s 后回复"
            )
            time.sleep(delay)
            # 标记首次 fire 完成（让下次走快路径）
            with self._lock:
                self._unb_first_fire_done.add(toid)

            # 10. 限流后 check
            if not self._check_rate_limit():
                logger.warning(f"ReplyScheduler 延迟后超额 cid={cid}")
                # V16.5 全链路日志：L6 延迟后再次超额（更严格）
                try:
                    from app.core.runtime_log import log_event
                    unb = last_msg.get("send_user_id", "-") if last_msg else "-"
                    log_event("RATE_LIMIT", "ERROR", cid, str(unb),
                              "rate limit still exceeded after delay",
                              reason="post_delay_still_exceeded",
                              threshold=self._rate_limit,
                              recent_count=len(self._recent_send_ts))
                except Exception:
                    pass
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
                from app.services.xianyu.message_guard import XianyuMessageGuard
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
            self._mark_seen_any(messages)  # V16.7: 合并标记所有
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
        """从事件里提 message text（兼容 goofish 多形状）

        实际 goofish message watch stdout 格式（实测）：
        {
          "event": "message",
          "cid": "...",
          "send_user_id": "...",
          "send_user_name": "...",
          "send_message": "在吗"   ← 文本直接在这里
        }

        也兼容 history 接口格式：
        {
          "message": {"text": {"text": "在吗"}}
        }
        """
        # 格式 1: goofish message watch 直接用 send_message 字段
        send_msg = event.get("send_message")
        if send_msg and isinstance(send_msg, str) and send_msg.strip():
            return send_msg
        # 格式 2: message history 用 message.text.text 嵌套
        msg = event.get("message") or {}
        if isinstance(msg, dict):
            text = msg.get("text") or {}
            if isinstance(text, dict):
                return text.get("text", "") or ""
            if text:
                return str(text)
        # 格式 3: 顶层 text 字段
        text = event.get("text")
        if text and isinstance(text, str):
            return text
        return ""

    # =============================================================
    # 内部：随机延迟
    # =============================================================
    def _compute_delay(self) -> float:
        """区间内均匀随机（V16.3.1：首次 10-15s / 后续 0-3s）

        修复：首次走 _first_min/max（默认 10-15s），后续走 _next_min/max（默认 0-3s）
        """
        with self._lock:
            is_first = len(self._unb_first_fire_done) == 0
        if is_first:
            return random.uniform(self._first_min_delay, self._first_max_delay)
        else:
            return random.uniform(self._next_min_delay, self._next_max_delay)

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

        智能客服 /chat SSE 实际格式（实测 19:36）：
          id: N
          data: {"type": "meta", ...}
          id: N+1
          data: {"type": "token", "text": "..."}
          ... (多个 token chunks)
          id: M
          data: {"type": "done", "session_id": "...", "assistant_message_id": N}
          data: {"type": "closed"}
        """
        try:
            payload = {
                "query": message,
                "user_id": None,
                "channel": "xianyu",
                "buyer_unb": unb,
                "context_block": context,
                "system_prompt": context,
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
            # 解析 SSE（多种格式兼容 + 多行 chunk 兼容）
            # SSE 实际格式（智能客服 /chat 实测）：
            #   id: 1
            #   data: {"type": "token", "text": "..."}
            #
            #   id: 2
            #   data: {"type": "token", "text": "..."}
            full_text = ""
            for chunk in resp.text.split("\n\n"):
                # chunk 可能是 "id: 1\ndata: {...}"（多行）
                # 提取 data: 行
                data_line = None
                for line in chunk.split("\n"):
                    line = line.strip()
                    if line.startswith("data:"):
                        data_line = line[5:].strip()
                        break
                if data_line is None:
                    continue
                payload_str = data_line
                if not payload_str or payload_str == "[DONE]":
                    continue
                try:
                    obj = _json.loads(payload_str)
                except (_json.JSONDecodeError, ValueError):
                    continue
                # 格式 1: {"type": "token", "text": "..."} (实际格式)
                if obj.get("type") == "token" or obj.get("event") == "token":
                    full_text += obj.get("text", "") or obj.get("data", "")
                # 格式 2: {"type": "done", ...} 智能客服 done 事件
                elif obj.get("type") == "done" or obj.get("event") == "done":
                    ans = (
                        obj.get("answer", "")
                        or obj.get("data", {}).get("answer", "")
                    )
                    if ans and not full_text:
                        full_text = ans
                # 格式 3: 直接 {answer: "..."}（旧版）
                elif "answer" in obj and obj["answer"]:
                    full_text = obj["answer"]
            return full_text.strip()
        except Exception as e:
            logger.warning(
                f"/chat 调用失败: {type(e).__name__}: {e}"
            )
            return ""

    # =============================================================
    # 内部：send_message + 失败重试
    # =============================================================
    def _parse_rate_limit_wait(self, result: dict) -> Optional[float]:
        """V16.6: 解析限流响应里的剩余等待秒数

        实际响应：{'ok': False, 'error_type': 'RateLimitedError',
                  'message': '限流：bucket=message.write 每 60s 上限 1，再等 53.1s'}

        Returns:
            float: 等待秒数（含 0.5s buffer，最少 1s）
            None: 非限流错误或 message 解析不出秒数
        """
        if not result:
            return None
        error_type = result.get("error_type", "") or ""
        message = str(result.get("message", "") or result.get("error", "") or "")
        if error_type != "RateLimitedError" and "再等" not in message:
            return None
        m = self._RATE_LIMIT_PATTERN.search(message)
        if not m:
            return None
        try:
            sec = float(m.group(1))
            # +0.5s buffer 防边界；最少 1s（避免 0.x 秒浪费）
            return max(sec + 0.5, 1.0)
        except (ValueError, TypeError):
            return None

    def _send_with_retry(self, cid: str, toid: str, text: str) -> dict:
        """send_message + 智能退避（V16.6 fix / V16.8 P1-B 无限重试）

        区分限流 vs 普通错误：
        - 限流：从 error message 解析 "再等 X.Xs"，sleep 那个时间再重试
                （独立配额：99999 = 实际无限，daemon 不死就一定能发出）
        - 普通错误：5s/10s/20s/40s/60s 指数退避（cap 60s，无限循环）

        V16.5 fix: 错误日志显示完整 result
        V16.6 fix: 限流场景下不立即放弃，等剩余窗口过
        V16.8 P1-B: 旧版 5 次后放弃 → 用户消息丢失。现无限重试，daemon stop 才停
        """
        max_attempts = self._max_retries + 1   # 非限流尝试配额（默认 3）
        rate_limit_left = self._RATE_LIMIT_MAX_RETRIES  # 限流额外配额（99999）
        last_result: dict = {"error": "no_attempt"}
        non_rl_count = 0  # 非限流尝试计数

        # V16.8 P1-B: 无限重试（直到 daemon stop 被外部触发）
        # 兜底逻辑：限流配额耗尽也只是日志告警不退出（生产中 1 分钟 / 50 配额够用 8 小时重试）
        while True:
            result = self._client.send_message(
                cid=cid, toid=toid, text=text
            )
            # V16.5 fix: 成功判定 — ok=True 即认为成功（message_id 可能为 None）
            if result.get("ok") is True or result.get("message_id"):
                return result
            last_result = result

            # 限流路径：解析剩余秒数，等到窗口过再发（不消耗非限流配额）
            wait_sec = self._parse_rate_limit_wait(result)
            if wait_sec is not None and rate_limit_left > 0:
                rate_limit_left -= 1
                logger.warning(
                    f"send_message 限流 等 {wait_sec:.1f}s "
                    f"(限流配额剩 {rate_limit_left}/{self._RATE_LIMIT_MAX_RETRIES}): "
                    f"result={result}"
                )
                time.sleep(wait_sec)
                continue
            # 限流配额耗尽 — 日志告警但继续（实际生产中限流配额 99999 不会出现耗尽）
            if wait_sec is not None:
                logger.error(
                    f"send_message 限流配额 {self._RATE_LIMIT_MAX_RETRIES} 用尽，"
                    f"但继续重试（daemon 不死就不放弃）: result={result}"
                )
                time.sleep(wait_sec)
                continue

            # 非限流路径：5s/10s/20s/40s/60s 指数退避（V16.8 P1-B）
            non_rl_count += 1
            wait = min(
                self._NON_RATE_LIMIT_BACKOFF_BASE_SEC * (2 ** (non_rl_count - 1)),
                self._NON_RATE_LIMIT_BACKOFF_MAX_SEC,
            )  # 5s, 10s, 20s, 40s, 60s, 60s, ...
            logger.warning(
                f"send_message 失败 (attempt {non_rl_count}) "
                f"等 {wait:.1f}s (V16.8 P1-B 5s→60s cap): result={result}"
            )
            time.sleep(wait)

        logger.error(
            f"ReplyScheduler cid={cid} 发送失败 (非限流{non_rl_count}次+"
            f"限流{self._RATE_LIMIT_MAX_RETRIES - rate_limit_left}次): "
            f"result={last_result}"
        )
        return last_result


__all__ = ["ReplyScheduler"]