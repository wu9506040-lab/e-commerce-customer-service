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
        rate_limit_per_minute: int = 6,  # V16.8 P1-F：默认 1/min → 6/min（IM 1s/条正常节奏）
        daily_quota: int = 50,
        max_retries: int = 2,
        chat_timeout: float = 60.0,
        db_session=None,  # V16.3 新增：用于落库 + 退款 handler
        seller_unb: str = "",  # V16.3 新增：cookie 账号 unb
        manual_mode=None,  # V18: ManualModeManager（人工接管，抄 XianyuAutoAgent）
        account_id: str = "a1",  # V18.2: 多账号归属
    ):
        self._client = xianyu_client
        self._manual_mode = manual_mode
        self._chat_api_url = chat_api_url
        # V16.8 P1-D: 首次 vs 后续统一延迟区间
        # 旧(V16.3.1)：首次 10-15s（"显得像真人"）—— 反功能：买家期望 < 3s 真人响应
        # 新：首次/后续都 0-3s — 真人客服 IM 响应节奏
        self._first_min_delay = float(os.environ.get("XIANYU_FIRST_MIN_DELAY", "0"))
        self._first_max_delay = float(os.environ.get("XIANYU_FIRST_MAX_DELAY", "3"))
        self._next_min_delay = float(os.environ.get("XIANYU_NEXT_MIN_DELAY", "0"))
        # V18-P0: 3 → 1s（真人感交给打字模拟，前置延迟只留阅读时间）
        self._next_max_delay = float(os.environ.get("XIANYU_NEXT_MAX_DELAY", "1"))
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
        # V18（2026-10-17）：per-cid 串行锁 — 买家在上一条回复还在生成时又发消息，
        # 两个 fire 并发会产生乱序/竞态回复。同 cid 串行：Q1 回完再答 Q2。
        # 清理策略抄 xianyu-auto-reply（上限 1w + 2h 过期）
        self._cid_locks: dict = {}
        self._cid_locks_guard = threading.Lock()
        # V18: 商品信息缓存（P2.3，抄 XianyuAutoAgent context_manager.items）
        self._item_cache = None
        # V18-P0: 打字模拟开关（XIANYU_SIMULATE_TYPING=0 关闭；默认开）
        self._simulate_typing = os.environ.get("XIANYU_SIMULATE_TYPING", "1") != "0"
        # V18.2: 多账号归属
        self.account_id = account_id
        # V18: 人工后台事件钩子（AdminBridge 注入；None=未启用）
        self._event_sink = None

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
        """BuyerProfile 单例(内部缓存) — 避免每条消息 new

        V18 P3：画像数据源 mtop 化 —— 用 ws client 的 XianyuApis
        （cookie.txt + 自签名，替代已弃用的 goofish MCP）。
        backend 容器内无 cookie，MtopProfileClient 自动降级禁用。
        """
        if self._buyer_profile_singleton is None:
            from app.services.xianyu.buyer_profile import (
                BuyerProfile, MtopProfileClient,
            )
            apis = getattr(self._client, "_apis", None)
            self._buyer_profile_singleton = BuyerProfile(
                client=MtopProfileClient(apis=apis)
            )
        return self._buyer_profile_singleton
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
    # V18-P0：问候前缀剥离（实测"在吗，这个能上飞机吗"被后端规则判成 chitchat
    # 返回"在的~请问想咨询什么"——合批真实问题被"在吗"前缀污染）
    _GREETING_PREFIX_RE = re.compile(
        r"^(在吗|在么|在不在|你好|您好|hi|hello|hey|早上好|下午好|晚上好)"
        r"[?？!！。~～，,\s]*",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_greeting_prefix(cls, text: str) -> str:
        """剥掉开头的问候词（可叠加），留实质问题；纯问候返回空串"""
        t = (text or "").strip()
        prev = None
        while prev != t and t:
            prev = t
            t2 = cls._GREETING_PREFIX_RE.sub("", t, count=1).strip()
            t = t2  # 纯问候剥完为空 → 循环结束返回空（调用方判空跳过）
        return t

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
        # V18-P0：剥问候前缀（"在吗，这个能上飞机吗"→"这个能上飞机吗"）+
        # 去空（纯问候剥空后丢弃，保留顺序）
        stripped = [self._strip_greeting_prefix(t) for t in texts]
        kept = [t.strip() for t in stripped if t and t.strip()]
        if kept:
            texts = kept
        # V18.2 修正：整批都是纯问候（"在吗"/"你好"）→ 保留原文走后端
        # chitchat 短路回"在的~"（此前剥空直接返空 query → 纯问候完全不回复）
        if not texts:
            texts = [t.strip() for t in (self._extract_text(m) for m in messages) if t and t.strip()]
            if not texts:
                return "", False
            logger.info("V18.2 整批纯问候 → 保留原文走 chitchat 应答")
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

    # V18：纯问候语判定（闲聊分诊用；整条只有问候词才算，"你好，容量多大"不算）
    _GREETING_RE = re.compile(
        r"^(在吗|在么|在不在|你好|您好|hi|hello|hey|早上好|下午好|晚上好)"
        r"[?？!！。~～，,\s]*$",
        re.IGNORECASE,
    )

    # V18：噪声消息判定（抄 XianyuAutoAgent.is_bracket_system_message）
    # - 中括号包裹：[去支付]/[去评价]/[信息卡片] 等系统卡片
    # - MsgTips：xianyu-auto-reply 同款营销提示标记
    _NOISE_RE = re.compile(r"^\s*\[[^\]]{1,30}\]\s*$")

    @classmethod
    def _is_noise_text(cls, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        if cls._NOISE_RE.match(t):
            return True
        if "MsgTips" in t or "msgArg1" in t:
            return True
        return False

    @classmethod
    def _is_greeting_text(cls, text: str) -> bool:
        """V18：整条消息是纯问候（无实质问题）"""
        t = (text or "").strip()
        return bool(t) and bool(cls._GREETING_RE.match(t))

    # V18-P0：本地分 Agent 关键词表（抄 XianyuAutoAgent.IntentRouter +
    # ai_reply_engine.detect_intent，按充电宝品类扩充 tech 词）
    _PRICE_KEYWORDS = (
        "便宜", "优惠", "刀", "降价", "价格", "多少钱", "价",
        "能少", "还能", "最低", "底价", "砍价", "少点", "包个邮", "让点",
    )
    _TECH_KEYWORDS = (
        "参数", "规格", "型号", "容量", "续航", "充电", "接口", "功率",
        "重量", "尺寸", "民航", "飞机", "快充", "电池", "几个月", "质保",
        "怎么用", "说明书", "功能", "用法", "驱动", "坏了", "故障", "设置",
        "连接", "对比", "适配",
    )

    @classmethod
    def _build_intent_prompt(cls, text: str, bargain_count: int) -> str:
        """本地分 Agent 短 prompt（抄两个参考项目的 per-intent prompt 风格）

        price/tech 关键词可叠加（合批"容量多大+能便宜点吗"两个 prompt 都挂）；
        都不命中走 default。议价轮次进 price prompt（梯度让步话术）。
        """
        t = (text or "").lower()
        is_price = any(kw in t for kw in cls._PRICE_KEYWORDS)
        is_tech = any(kw in t for kw in cls._TECH_KEYWORDS)
        parts = []
        if is_price:
            # V18-P0.1：与 yaml"不二价框架"对齐——梯度走赠品加码而非降价
            # （此前"价格梯度让步"与人设"不让价"打架，LLM 随机站队）
            parts.append(
                "【议价模式】价格不让（不二价），用赠品补偿守价。梯度："
                "第1次送数据线；第2次加 18W 充电头；第3次赠品拉满并促单"
                "（拍下改价马上发）；出价过低果断拒绝，强调成色与价值。"
                "简短直接，每句≤10字，总字数≤40字。"
                + (f"当前是第{bargain_count}轮。" if bargain_count else "")
            )
        if is_tech:
            parts.append(
                "【技术咨询模式】你是技术专家，基于【商品信息】回答产品功能/"
                "使用方法/注意事项。简短专业，每句≤10字，总字数≤40字，"
                "避免过度承诺。"
            )
        if not parts:
            parts.append(
                "【通用客服模式】你是资深电商卖家，聚焦商品介绍/物流/售后/"
                "使用体验。简短友好，每句≤10字，总字数≤40字。"
                "如已谈妥价格，引导下单（确认要的话今天发货）。"
            )
        return "\n".join(parts)

    def _mark_seen_any(self, messages: list[dict]) -> None:
        """V16.7: 标记所有消息为 seen"""
        for m in messages:
            self._mark_seen(m)

    def _get_cid_lock(self, cid: str) -> threading.Lock:
        """V18: 取（或建）per-cid 串行锁

        清理策略抄 xianyu-auto-reply ai_reply_engine._cleanup_expired_chat_locks：
        锁上限 1w，超过清掉 2h 未用的（防慢性泄漏）
        """
        now = time.time()
        with self._cid_locks_guard:
            if len(self._cid_locks) > 10000:
                expire = 7200.0
                stale = [
                    k for k, (_, ts) in self._cid_locks.items()
                    if now - ts > expire
                ]
                for k in stale:
                    self._cid_locks.pop(k, None)
                logger.info(f"ReplyScheduler 清理过期 cid 锁 {len(stale)} 个")
            entry = self._cid_locks.get(cid)
            if entry is None:
                lock = threading.Lock()
                self._cid_locks[cid] = (lock, now)
                return lock
            lock, _ = entry
            self._cid_locks[cid] = (lock, now)  # 刷新使用时间
            return lock

    def _handle_fire(self, cid: str, messages: list[dict]) -> None:
        """fire 入口 — 同 cid 串行（V18），防并发回复乱序；不同 cid 互不阻塞"""
        with self._get_cid_lock(str(cid)):
            self._handle_fire_impl(cid, messages)

    def _handle_fire_impl(self, cid: str, messages: list[dict]) -> None:
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

        # V16.7: 多消息合并 — 拼成单条 query
        # V18 fix（2026-10-17）：原"任一已见则跳过整批"有致命组合缺陷——
        # 闲鱼重推（重连/ackDiff）会把已回复的老消息混进新消息同一批，
        # 任一命中 seen → 整批丢弃 → 买家的新消息永远没人回
        # （实测 13:46 买家连发 3 条全部被 13:46:15 已回复的"在吗"重推污染丢弃）。
        # 改为：剔除已见消息，只处理剩下的；全见才跳过。
        fresh_messages = [m for m in messages if not self._is_duplicate(m)]
        if not fresh_messages:
            logger.info(f"ReplyScheduler 重复消息跳过 cid={cid} batch={len(messages)}")
            return
        if len(fresh_messages) < len(messages):
            logger.info(
                f"ReplyScheduler 剔除已见 {len(messages) - len(fresh_messages)} 条 "
                f"cid={cid}，处理剩余 {len(fresh_messages)} 条"
            )

        # V18（2026-10-17）：噪声过滤（抄 XianyuAutoAgent.is_bracket_system_message）
        # 中括号系统消息（[去支付]/[去评价]/[信息卡片]）不回复，直接标记已见
        _noise = [m for m in fresh_messages
                  if self._is_noise_text(self._extract_text(m))]
        if _noise:
            self._mark_seen_any(_noise)
            fresh_messages = [m for m in fresh_messages if m not in _noise]
            logger.info(
                f"ReplyScheduler 过滤系统噪声 {len(_noise)} 条 cid={cid}，"
                f"剩余 {len(fresh_messages)} 条"
            )
            if not fresh_messages:
                return

        # 2.5 V18: 人工接管检查（抄 XianyuAutoAgent is_manual_mode）——
        # 卖家接管期间买家消息只标记已见，不自动回复（卖家自己答）
        if self._manual_mode is not None:
            try:
                if self._manual_mode.is_manual(cid):
                    logger.info(f"ReplyScheduler 人工接管中，跳过自动回复 cid={cid}")
                    self._mark_seen_any(fresh_messages)
                    return
            except Exception as e:
                logger.warning(f"ReplyScheduler 人工接管检查失败: {e}")

        # V18（2026-10-17）：智能分诊 — 问候与实质问题分开处理（纯规则，零 LLM 开销）
        # - 有实质问题：丢弃纯问候（"在吗/你好"），只合并实质问题答——买家
        #   "在吗+容量多大+能便宜点吗" 应答容量+价格，不回"在的~"空话
        # - 全是问候：只回最后一条（避免连发 3 条"在吗"回 3 次"在的~"）
        # - 退款意图仍走 step 4 的专用 handler（高风险，单独回复）
        _greetings = [m for m in fresh_messages
                      if self._is_greeting_text(self._extract_text(m))]
        _substantive = [m for m in fresh_messages if m not in _greetings]
        if _substantive:
            if _greetings:
                logger.info(
                    f"ReplyScheduler 分诊 cid={cid}：丢弃问候 {len(_greetings)} 条，"
                    f"合并实质问题 {len(_substantive)} 条"
                )
            messages = _substantive
        elif len(_greetings) < len(fresh_messages):
            messages = [_greetings[-1]]
        else:
            messages = fresh_messages
        fresh_messages = messages
        last_msg = messages[-1] if messages else {}
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
        # V16.8 P2-A hotfix: cid/toid 强制 str（MCP 工具 message_send 类型校验：
        # cid "Input should be a valid string [type=string_type, input_value=40703180655, input_type=int]"）
        if cid is not None:
            cid = str(cid)
        if toid:
            toid = str(toid)
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
        self._emit("buyer_message", {
            "cid": cid, "text": last_text[:200], "batch": len(messages),
            "account_id": self.account_id,
        })

        # 4. **V16.3 退款意图检测**（基于 DB）
        if self._db is not None and self._seller_unb and self._is_refund_intent_any(messages):
            try:
                from app.services.xianyu.refund_handler import XianyuRefundHandler
                handler = XianyuRefundHandler(db=self._db, seller_unb=self._seller_unb)
                # 拉 buyer profile 拿 nick（V18: 非阻塞，同 step 5）
                try:
                    profile = BuyerProfile().get_profile_nowait(unb=toid)
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
        # V18（2026-10-17）：改非阻塞——get_profile 同步拉 30s+ 是首响 54s 的最大头。
        # 缓存未命中时立刻返空画像 + 后台线程补拉，本轮 /chat 不等画像；
        # 拉完写缓存，同买家下一轮消息自动带上画像上下文。
        try:
            profile = self._get_buyer_profile().get_profile_nowait(unb=toid)
            profile_ctx = profile.to_prompt_context()
            nick = profile.nick
            is_returning = profile.is_returning_buyer
        except Exception as e:
            logger.warning(f"ReplyScheduler BuyerProfile 失败: {e}")
            profile_ctx = ""
            nick = None
            is_returning = False

        # 5.5 V18: 商品信息（P2.3 抄 XianyuAutoAgent——本地缓存，miss 才走 mtop）
        _item_desc = ""
        try:
            _item_id = str(last_msg.get("item_id", "") or "")
            if _item_id:
                from app.services.xianyu.item_cache import ItemCache
                if self._item_cache is None:
                    self._item_cache = ItemCache()
                _apis = getattr(self._client, "_apis", None)
                _item_desc = self._item_cache.get_item_description(_item_id, _apis)
                if _item_desc:
                    logger.info(
                        f"ReplyScheduler 商品信息就绪 item={_item_id} "
                        f"len={len(_item_desc)}"
                    )
        except Exception as e:
            logger.warning(f"ReplyScheduler 商品信息获取失败: {e}")

        # 6. EscalationDetector（V16.5 lazy 单例）
        try:
            escalation = self._get_escalation_detector()
            esc_result = escalation.should_escalate(unb=toid, message=last_text)
            escalation.record_message(unb=toid)
        except Exception as e:
            logger.warning(f"ReplyScheduler EscalationDetector 失败: {e}")
            esc_result = None

        # 6.5 V18: 议价轮次计数（抄 XianyuAutoAgent context_manager +
        # xianyu-auto-reply ai_reply_engine.detect_intent 本地价格关键词）
        _bargain_count = 0
        try:
            from app.services.xianyu.bargain_counter import (
                get_bargain_counter, is_price_message,
            )
            _counter = get_bargain_counter()
            if any(is_price_message(self._extract_text(m)) for m in messages):
                _bargain_count = _counter.increment(cid)
                logger.info(
                    f"ReplyScheduler 议价轮次 cid={cid} count={_bargain_count}"
                )
            else:
                _bargain_count = _counter.get_count(cid)
        except Exception as e:
            logger.warning(f"ReplyScheduler 议价计数失败: {e}")

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
            # V18-P0（2026-10-17）：话术优化三件套（对齐两个参考项目）——
            #   1) 本地分 Agent：price/tech/default 纯关键词路由（抄
            #      XianyuAutoAgent.IntentRouter + ai_reply_engine.detect_intent，
            #      零 LLM 开销），各挂独立短 prompt
            #   2) 字数收紧 ≤40 字（两个参考项目一致的标准："每句≤10字，总字数≤40字"）
            #   3) 议价梯度话术进 price prompt（抄 price_prompt_example 回答逻辑）
            _sys_base = (
                "你是智能客服 Agent。回复要求：像真人卖家发 IM，口语化短句，"
                "每句不超过 10 字、一次回复总字数不超过 40 字，只回答买家问到的点；"
                "多条消息合答时逐点简短回应，不要罗列卖点清单、不要写 markdown 标题。"
                "若消息是系统卡片/任务提醒/与交易完全无关的问题"
                "（如问你是谁、什么模型、让他做什么操作），只回复 [skip]，不要解释。"
            )

            # 本地分 Agent（tech 关键词抄 IntentRouter + 补充电宝品类词；
            # price 关键词抄 ai_reply_engine；price 优先——合批"容量+砍价"
            # 时议价策略更需要显式注入，tech 由【商品信息】覆盖）
            _texts_all = " ".join(
                self._strip_greeting_prefix(self._extract_text(m))
                for m in messages
            )
            _intent_prompt = self._build_intent_prompt(_texts_all, _bargain_count)
            if _intent_prompt:
                _sys_base += "\n" + _intent_prompt

            # V18: 议价轮次注入（梯度让步策略已并入 price prompt）
            if _bargain_count > 0:
                _sys_base += f"\n▲当前议价轮次：{_bargain_count}"

            # V18: 商品信息注入（P2.3，抄 main.build_item_description 输出）
            if _item_desc:
                _sys_base += f"\n【商品信息】{_item_desc}"
            try:
                prompts = self._get_prompts()
                system_prompt = prompts.render(
                    scenario=scenario_result.scenario.value,
                    system_base=_sys_base,
                    buyer_profile=profile_ctx,
                    nick=nick,
                )
            except Exception as e:
                logger.warning(f"ReplyScheduler XianyuPrompts 失败: {e}")
                system_prompt = f"{_sys_base}\n{profile_ctx}"

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

            # 11. /chat（V18: 传 cid → 稳定 session，多轮追问有上下文）
            reply = self._call_chat_api(
                unb=toid,
                message=last_text,
                context=system_prompt,
                cid=cid,
            )
            if not reply:
                # V18-P0: LLM 返空不再静默（买家干等）——发拟人兜底话术，
                # seen 正常标记（重推不会重复回复）
                logger.warning(f"ReplyScheduler /chat 返空 cid={cid} → 发兜底话术")
                reply = "稍等，我看一下哈"

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

            # 12.5 V18: no_reply 约定（抄 XianyuAutoAgent "返回 '-' 不回复"）
            # system prompt 已指示：系统卡片/无关问题（你是谁/什么模型）回复 [skip]
            if text_to_send.strip() == "[skip]":
                logger.info(f"ReplyScheduler LLM 判定无需回复 cid={cid}")
                self._mark_seen_any(messages)
                return

            # 12.6 V18: 站外引流安全过滤（抄 XianyuAutoAgent._safe_filter）
            from app.services.xianyu.message_guard import sanitize_off_platform
            text_to_send = sanitize_off_platform(text_to_send)

            # 12.7 V18-P0：数据校验管道（markdown/角标/占位符/长度/价格幻觉）
            # —— reply_validator 统一处理，含原 [数字] 角标剥离
            try:
                from app.services.xianyu.reply_validator import (
                    extract_allowed_prices, validate_reply,
                )
                _nick_for_val = nick if (nick and nick != "朋友") else None
                text_to_send, _issues = validate_reply(
                    text_to_send,
                    nick=_nick_for_val,
                    allowed_prices=(
                        extract_allowed_prices(_item_desc) if _item_desc else None
                    ),
                )
                if _issues:
                    logger.warning(
                        f"ReplyScheduler 数据校验命中 {len(_issues)} 项 cid={cid}: "
                        f"{_issues}"
                    )
            except Exception as e:
                # 校验器异常不阻塞发送（降级为原清洗逻辑）
                logger.warning(f"ReplyValidator 失败: {e}")
                _cleaned = re.sub(r"\s*\[\d{1,2}\]", "", text_to_send)
                _cleaned = re.sub(r"\n{2,}", "\n", _cleaned).strip()
                if _cleaned:
                    text_to_send = _cleaned
            if not text_to_send.strip():
                # 兜底：校验后为空也发拟人话术（绝不发空消息）
                text_to_send = "稍等，我看一下哈"

            # 12.75 V18.2.1：退款诉求 → 先查订单证据（买家是否真的下过单）
            # V18.2.2：平台直接申请退款（聊天框出现"我发起了退款申请"卡片）
            # → 视为已下单（平台动作即证据），改写受理话术 + P0 工单
            _platform_refund = any(k in (last_text or '') for k in ('发起了退款申请', '申请退款', '发起退款'))
            _refund_asked = _platform_refund or any(k in (last_text or '') for k in ('退款', '退货', '退钱'))
            _order_ev = None
            if _refund_asked and getattr(self, '_recorder', None) is not None:
                try:
                    _order_ev = self._recorder.lookup_order_evidence(cid)
                except Exception as oe:
                    logger.warning(f'订单证据查询失败: {oe}')
                if _platform_refund:
                    # 平台退款申请 = 已下单铁证：改写为受理话术
                    text_to_send = '亲，您的退款申请已收到，我们会尽快处理，处理好后会第一时间联系您～'
                    logger.info(f'ReplyScheduler 平台退款申请 → P0 受理话术 cid={cid}')
                elif _order_ev is not None and not _order_ev.get('has_order'):
                    # 无付款记录 → 不转专员，直接告知未查到订单
                    text_to_send = '亲，没查到您的订单记录哦，您还没有拍下付款呢～下单后有问题随时找我'
                    logger.info(f'ReplyScheduler 退款诉求但无订单记录 → 改写回复 cid={cid}')

            # 12.8 V18.2：转人工/退款/投诉 → 自动生成工单（坐席工作台可见）
            # 触发：① EscalationDetector 判定升级 ② 回复含转人工话术标记
            # （后端退款 handler 的"转接专员"话术也在此捕获）
            try:
                self._maybe_create_ticket(
                    cid=cid, toid=toid,
                    buyer_text=last_text, reply_text=text_to_send,
                    escalated=bool(esc_result and esc_result.escalate),
                    esc_reason=(esc_result.reason.value if esc_result and esc_result.reason else None),
                    order_evidence=_order_ev,
                    refund_asked=_refund_asked,
                )
            except Exception as e:
                logger.warning(f"ReplyScheduler 工单生成失败: {e}")

        # 13. send_message 真发
        # V18-P0（2026-10-17）：打字模拟 + 长回复分段（抄 XianyuAutoAgent
        # main.py simulate_human_typing 的"长度比例延迟"思路，参数收紧）：
        # - 参考是 0.1-0.3s/字上限 10s——太慢，与我们提速目标冲突
        # - 收紧为 0.04s/字（env XIANYU_TYPING_CHAR_SEC）、上限 3s、总开关
        #   XIANYU_SIMULATE_TYPING（默认开）
        # - >48 字在句界拆两条发送，间隔 0.6-1.2s——真人 IM 节奏
        _parts = self._split_for_typing(text_to_send)
        _results_ok = True
        _last_message_id = None
        for pi, part in enumerate(_parts):
            if self._simulate_typing:
                _td = self._typing_delay(len(part))
                logger.info(
                    f"ReplyScheduler 打字模拟 {_td:.1f}s (len={len(part)}) cid={cid}"
                )
                time.sleep(_td)
            elif pi > 0:
                time.sleep(random.uniform(0.4, 0.8))
            result = self._send_with_retry(cid=cid, toid=toid, text=part)
            _last_message_id = result.get("message_id")
            if not (result.get("ok") or result.get("message_id")):
                _results_ok = False
                logger.error(
                    f"ReplyScheduler cid={cid} 发送失败(第{pi+1}段): {result.get('error')}"
                )
                break
        if _results_ok:
            self._record_sent()
            self._mark_seen_any(messages)  # V16.7: 合并标记所有
            logger.info(
                f"ReplyScheduler cid={cid} 已发 {len(_parts)} 段 "
                f"message_id={_last_message_id}"
            )
            self._emit("reply_sent", {
                "cid": cid, "text": text_to_send[:200], "parts": len(_parts),
                "account_id": self.account_id,
            })
            # V18.2: AI 回复实时落库（/chat 写穿已对 xianyu 渠道跳过，归属 daemon）
            _rec = getattr(self, "_recorder", None)
            if _rec is not None:
                try:
                    _rec.record_ai(cid, text_to_send, account=self.account_id)
                except Exception as rec_e:
                    logger.debug(f"recorder.record_ai 失败: {rec_e}")

    # V18.2：转人工工单触发标记
    _HANDOFF_REPLY_MARKERS = ("转接专员", "转人工", "人工客服", "专员", "工作人员")
    _HANDOFF_BUYER_MARKERS = ("人工", "投诉", "退款", "退货", "找人工")

    def _maybe_create_ticket(self, cid: str, toid: str, buyer_text: str,
                             reply_text: str, escalated: bool = False,
                             esc_reason: str | None = None,
                             order_evidence: dict | None = None,
                             refund_asked: bool = False) -> int | None:
        """转人工场景自动生成工单（V18.2 / V18.2.1 带订单证据与可读摘要）

        触发：EscalationDetector 升级 / 回复含转人工话术 / 买家提退款退货投诉。
        例外：退款诉求但查无订单 → 不建单（已直接回复买家）。
        幂等：persist_handoff 对同 session 未结单工单自动合并，不重复建单。
        """
        buyer_text = buyer_text or ""
        reply_text = reply_text or ""
        hit_reply = any(k in reply_text for k in self._HANDOFF_REPLY_MARKERS)
        hit_buyer = any(k in buyer_text for k in self._HANDOFF_BUYER_MARKERS)
        if not (escalated or hit_reply or hit_buyer):
            return None
        # V18.2.2：平台直接申请退款（"我发起了退款申请"卡片）→ P0 铁单，
        # 平台动作即下单铁证，绕过无单检查
        platform_refund = any(k in buyer_text for k in ("发起了退款申请", "申请退款", "发起退款"))
        if platform_refund:
            escalated = True
        elif refund_asked and order_evidence is not None and not order_evidence.get("has_order"):
            logger.info(f'退款诉求但无订单记录 → 不建退款工单 cid={cid}')
            return None

        from app.services.handoff_ticket_service import persist_handoff

        if platform_refund:
            reason = "platform_refund"
            priority = "P0"
        else:
            reason = esc_reason or ("refund_request" if hit_buyer and ("退款" in buyer_text or "退货" in buyer_text) else "user_requested")
            priority = "P1" if ("退款" in buyer_text or "退货" in buyer_text or "投诉" in buyer_text) else "P2"
        ticket_id = persist_handoff(
            user_id=self._uid(toid),
            session_id=f"xianyu-{cid}",
            reason=reason,
            payload_dict={
                "channel": "xianyu",
                "account": self.account_id,
                "cid": cid,
                "buyer_unb": toid,
                "buyer_text": buyer_text[:300],
                "ai_reply": reply_text[:300],
                "escalated": escalated,
                "order_evidence": order_evidence or {},
                "summary_text": (
                    f"【闲鱼转人工】诉求：{buyer_text[:60]} | "
                    f"订单状态：{(order_evidence or {}).get('stage') or '未查到订单记录'} | "
                    f"最近对话：{' / '.join((order_evidence or {}).get('recent_history', [])[-3:])}"
                ),
            },
            category="闲鱼转人工",
            priority=priority,
            matched_keyword=(buyer_text[:30] if hit_buyer else (reply_text[:30] if hit_reply else None)),
        )
        if ticket_id:
            logger.info(
                f"ReplyScheduler 工单已生成 id={ticket_id} cid={cid} "
                f"reason={reason} priority={priority}"
            )
            # V18.2.3: 外发通知（钉钉/企微/飞书/微信推送，配置 config/notify.json）
            try:
                from app.services.xianyu.notification_service import push_async
                push_async(
                    f"【闲鱼工单 {priority}】{reason}",
                    f"买家({cid}): {buyer_text[:120]} | AI 回复: {reply_text[:120]}",
                    event_key=f"ticket:{cid}",
                    priority=priority,
                )
            except Exception as ne:
                logger.debug(f'外发通知失败: {ne}')
        return ticket_id

    @staticmethod
    def _uid(toid: str) -> int:
        t = str(toid or "")
        return int(t) if t.isdigit() else 0

    def _emit(self, kind: str, data: dict) -> None:
        """V18: 向人工后台发布事件（best-effort，失败不影响主链路）"""
        sink = getattr(self, "_event_sink", None)
        if sink is None:
            return
        try:
            sink(kind, data)
        except Exception as e:
            logger.debug(f"_event_sink 失败: {e}")

    # 打字模拟参数（V18-P0）
    _TYPING_CHAR_SEC = float(os.environ.get("XIANYU_TYPING_CHAR_SEC", "0.04"))
    _TYPING_MAX_SEC = float(os.environ.get("XIANYU_TYPING_MAX", "3.0"))
    _TYPING_SPLIT_LEN = 48

    @classmethod
    def _typing_delay(cls, text_len: int) -> float:
        """打字延迟 = 0.2-0.8s 基础 + 字数×0.04s，上限 3s（抄参考长度比例思路）"""
        base = random.uniform(0.2, 0.8)
        return min(base + text_len * cls._TYPING_CHAR_SEC, cls._TYPING_MAX_SEC)

    @staticmethod
    def _split_for_typing(text: str) -> list[str]:
        """>48 字在句界就近拆两条（找不到句界则中点硬拆）"""
        t = (text or "").strip()
        if len(t) <= ReplyScheduler._TYPING_SPLIT_LEN:
            return [t]
        mid = len(t) // 2
        best = None
        for m in re.finditer(r"[。！？!?~～\n]", t):
            if abs(m.start() - mid) < (abs(best - mid) if best is not None else 10**9):
                best = m.start() + 1
        cut = best if best is not None else mid
        a, b = t[:cut].strip(), t[cut:].strip()
        return [p for p in (a, b) if p] or [t]

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
    def _call_chat_api(self, unb: str, message: str, context: str, cid: str = "") -> str:
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
            # V18（2026-10-17）：稳定会话 — 同一 cid 固定 session_id，
            # 多轮对话历史在 /chat 侧累积。此前每次 fire 都是全新 session，
            # 买家隔一分钟追问"那重量呢"，LLM 根本不知道"那"指什么。
            if cid:
                payload["session_id"] = f"xianyu-{cid}"
            # V17 修复：Windows 默认 locale=GBK，httpx.json= 会把中文按 GBK 编码
            # 后端 FastAPI 收到 GBK 字节按 UTF-8 decode 失败 → 400 "error parsing the body"
            # 显式用 UTF-8 编码 + ensure_ascii=False 保留中文
            import json as _json
            body_bytes = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
            resp = httpx.post(
                self._chat_api_url,
                content=body_bytes,
                headers={"Content-Type": "application/json"},
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

        # V18: 不用 goofish-cli，直接走 WebSocket LWP 发消息
        # 参考 shaxiu main.py:112-156 + zhinianboke xianyu_async.py:1990-2073
        # 优势：不需要 mtop session、不需要 cookies.json、不限 mtop bucket
        while True:
            result = self._client.send_text_message_sync(
                cid=cid, toid=toid, text=text
            )
            # V16.5 fix: 成功判定 — ok=True 即认为成功（message_id/mid 可能为 None）
            if result.get("ok") is True:
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