"""
业务指标埋点 — M8 可观测性

内存指标系统（不引入 Prometheus，CLAUDE.md 禁止新基础设施）：
- 计数器（threading.Lock 保护）
- 延迟直方图（最近 N 个样本，算 p50/p90/max）
- hit@K ring buffer（最近 100 次 RAG 检索的命中情况）
- /metrics 端点导出 JSON snapshot

API:
- inc_chat(intent, v3_engine)        增加一次 chat 调用计数
- record_chat_latency(ms)            记录 chat 延迟
- record_answer_tokens(n)            记录 answer token 数（粗估）
- inc_retrieve_hits(n)               记录单次 RAG 检索的命中数
- inc_qdrant_search(result)          success / fallback_open / error
- inc_embedding(result)              success / retry / error
- record_hit_at_k(source_ranks)      记录 (query, source_top_ranks) 给 hit@K
- snapshot()                         返回完整 JSON（/metrics 端点用）

设计取舍：
- 用 threading.Lock 而非 asyncio.Lock：FastAPI 同步 worker 走线程池
- 直方图固定 1000 样本（环形），避免内存爆炸
- hit@K 用最近 100 次查询窗口（线上实时反映召回质量）
"""
import logging
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================
# 延迟分位数（手写，避免 numpy 依赖）
# =============================================================
def _percentile(samples: List[float], p: float) -> float:
    """简单分位数（线性插值）"""
    if not samples:
        return 0.0
    sorted_samples = sorted(samples)
    k = (len(sorted_samples) - 1) * p
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_samples) else f
    if f == c:
        return sorted_samples[f]
    return sorted_samples[f] + (sorted_samples[c] - sorted_samples[f]) * (k - f)


# =============================================================
# Metrics 单例
# =============================================================
class Metrics:
    """线程安全的内存指标收集器"""

    # 直方图采样窗口
    LATENCY_WINDOW = 1000
    # hit@K 评估窗口
    HIT_K_WINDOW = 100

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 启动时间
        self._started_at = time.time()

        # ----- chat 维度 -----
        self.chat_total = 0
        self.chat_by_intent: Dict[str, int] = {}
        self.chat_by_v3_engine: Dict[str, int] = {}
        self.chat_latency_ms: Deque[float] = deque(maxlen=self.LATENCY_WINDOW)
        self.chat_answer_tokens_total = 0
        self.chat_retrieve_hits_sum = 0  # 求和，snapshot 算 avg
        self.chat_retrieve_hits_count = 0  # 样本数

        # ----- RAG / qdrant -----
        self.qdrant_search_total = 0
        self.qdrant_search_success = 0
        self.qdrant_fallback_open_total = 0
        self.qdrant_error_total = 0

        # ----- embedding -----
        self.embedding_calls_total = 0
        self.embedding_retries_total = 0
        self.embedding_errors_total = 0

        # ----- hit@K ring buffer -----
        # 每条记录：[(source_1_rank, source_2_rank, ...)]   rank 从 1 开始，None=未命中
        # 简化：只记首个正例 source 在 top-K 中的位置（1-based，0=未命中）
        self._hit_k_window: Deque[int] = deque(maxlen=self.HIT_K_WINDOW)
        # 上限 + 实际样本数（deque 满后 len 就是 HIT_K_WINDOW）
        self._hit_k_total_samples = 0  # 历史累计（用来算总 hit@K）

        # ----- M11.5 P2：异常行为监控告警计数 -----
        # 5 类告警：ip_high_freq / ip_multi_account / user_high_freq / user_sku_probe / user_order_probe
        self.behavior_alerts_total = 0
        self.behavior_alerts_by_type: Dict[str, int] = {}

        # ----- M12：query 改写（指代补全） -----
        # reason: 'rewritten' | 'skipped_no_coref' | 'skipped_no_history' | 'error_empty' | 'error_too_long' | 'error_llm'
        self.rewrite_total = 0
        self.rewrite_by_reason: Dict[str, int] = {}

        # ----- Phase 4 A4：Multi-Query 多路改写 -----
        # reason: 'rewritten' | 'skipped_no_coref' | 'skipped_no_history'
        #         | 'parse_fail' | 'too_long' | 'llm_error' | 'too_few_variants'
        self.rewrite_multi_total = 0
        self.rewrite_multi_by_reason: Dict[str, int] = {}

        # ----- M14：OrderContextResolver 决策质量指标 -----
        # 4 类指标（plan §10 阶段 4）：
        # 1. multi_order_disambiguation_accuracy = SHOW_PICKER / total_orders_many
        #    （N>=2 订单时返 SHOW_PICKER 卡片让用户选的比例）
        # 2. no_order_no_completion_rate = ASK_LOGIN_OR_LIST / total_orders_zero
        #    （0 订单场景返 ASK_LOGIN_OR_LIST 而非误答的比例）
        # 3. card_triggered_when_expected_rate = card_sent_when_expected / card_expected
        #    （SSE meta.card 应发且实际发的比例）
        # 4. proactive_list_order_accuracy（与 #1 等价，复用同分母）
        # 设计：raw 计数 + snapshot 算 ratio，避免 in-flight 比率漂移
        self.resolver_total = 0
        self.resolver_by_action: Dict[str, int] = {}
        self.resolver_total_orders_zero = 0  # 0 订单
        self.resolver_total_orders_one = 0   # 1 订单
        self.resolver_total_orders_many = 0  # N>=2 订单
        self.resolver_card_expected = 0              # SSE Card 应发总次数
        self.resolver_card_sent_when_expected = 0    # 应发时实际发送的次数（仅计分子）

    # ----- chat -----

    def inc_chat(self, intent: str, v3_engine: str = "-") -> None:
        """记录一次 chat 调用

        v3_engine: "-" 表示非 V3 路径；"v2" / "v3" 表示 V3 开关下的 refund 分支
        """
        with self._lock:
            self.chat_total += 1
            self.chat_by_intent[intent] = self.chat_by_intent.get(intent, 0) + 1
            self.chat_by_v3_engine[v3_engine] = self.chat_by_v3_engine.get(v3_engine, 0) + 1

    def record_chat_latency(self, latency_ms: float) -> None:
        with self._lock:
            self.chat_latency_ms.append(latency_ms)

    def record_answer_tokens(self, n: int) -> None:
        with self._lock:
            self.chat_answer_tokens_total += n

    def record_retrieve_hits(self, hits: int) -> None:
        with self._lock:
            self.chat_retrieve_hits_sum += hits
            self.chat_retrieve_hits_count += 1

    # ----- RAG / qdrant -----

    def inc_qdrant_search(self, result: str) -> None:
        """result: 'success' | 'fallback_open' | 'error'"""
        with self._lock:
            self.qdrant_search_total += 1
            if result == "success":
                self.qdrant_search_success += 1
            elif result == "fallback_open":
                self.qdrant_fallback_open_total += 1
            elif result == "error":
                self.qdrant_error_total += 1
            else:
                logger.warning(f"unknown qdrant result: {result}")

    # ----- embedding -----

    def inc_embedding(self, result: str, retries: int = 0) -> None:
        """result: 'success' | 'retry' | 'error'"""
        with self._lock:
            self.embedding_calls_total += 1
            if result == "success":
                pass
            elif result == "retry":
                self.embedding_retries_total += 1
            elif result == "error":
                self.embedding_errors_total += 1
            else:
                logger.warning(f"unknown embedding result: {result}")
            # 重试次数单独累计（每次 retry 记一次）
            if retries:
                self.embedding_retries_total += retries

    # ----- hit@K -----

    def record_hit_at_k(self, rank: int) -> None:
        """记录一次 RAG 检索中，正例 source 出现在 top-K 的位置
        rank: 1-based，rank=0 表示未命中
        """
        with self._lock:
            self._hit_k_window.append(rank)
            self._hit_k_total_samples += 1

    # ----- M11.5 P2：异常行为告警 -----

    def inc_behavior_alert(self, alert_type: str) -> None:
        """记录一次行为监控告警（M11.5 P2）

        Args:
            alert_type: 告警类型（AlertType 常量之一）
        """
        with self._lock:
            self.behavior_alerts_total += 1
            self.behavior_alerts_by_type[alert_type] = (
                self.behavior_alerts_by_type.get(alert_type, 0) + 1
            )

    # ----- M12：query 改写 -----

    def inc_rewrite(self, reason: str) -> None:
        """记录一次 query 改写结果

        Args:
            reason: 'rewritten' | 'skipped_no_coref' | 'skipped_no_history' | 'error_*'
        """
        with self._lock:
            self.rewrite_total += 1
            self.rewrite_by_reason[reason] = self.rewrite_by_reason.get(reason, 0) + 1

    def inc_rewrite_multi(self, reason: str) -> None:
        """记录一次 Multi-Query 改写结果（Phase 4 A4）

        Args:
            reason: 'rewritten' | 'skipped_no_coref' | 'skipped_no_history'
                    | 'parse_fail' | 'too_long' | 'llm_error' | 'too_few_variants'
        """
        with self._lock:
            self.rewrite_multi_total += 1
            self.rewrite_multi_by_reason[reason] = self.rewrite_multi_by_reason.get(reason, 0) + 1

    # ----- M14：Resolver 决策质量 -----

    def inc_resolver_decision(
        self,
        action: str,
        total_orders: int,
        card_sent: bool,
        card_expected: bool,
    ) -> None:
        """记录一次 Resolver 决策（用于 4 类质量指标）。

        Args:
            action: OrderResolverAction.value（如 'show_picker' / 'direct_answer'）
            total_orders: 用户订单总数（0 / 1 / N）；gray 关闭时传 0
            card_sent: 本次 SSE meta.card 字段是否实际填充
            card_expected: 本次按规则是否应该发 card（SHOW_PICKER 或 N=1 时 True）
        """
        with self._lock:
            self.resolver_total += 1
            self.resolver_by_action[action] = self.resolver_by_action.get(action, 0) + 1
            if total_orders <= 0:
                self.resolver_total_orders_zero += 1
            elif total_orders == 1:
                self.resolver_total_orders_one += 1
            else:
                self.resolver_total_orders_many += 1
            if card_expected:
                self.resolver_card_expected += 1
                # 分子仅在 expected=True 且 sent=True 时累加（避免"非期望发送"膨胀分子）
                if card_sent:
                    self.resolver_card_sent_when_expected += 1

    # ----- snapshot -----

    def _hit_at_k(self, k: int) -> float:
        """计算窗口内 hit@K"""
        if not self._hit_k_window:
            return 0.0
        hits = sum(1 for r in self._hit_k_window if 1 <= r <= k)
        return round(hits / len(self._hit_k_window), 4)

    def snapshot(self, circuit_breaker_stats: Optional[Dict] = None) -> Dict:
        """导出完整指标快照（/metrics 端点用）

        Args:
            circuit_breaker_stats: {name: {state, failure_count}}
        """
        with self._lock:
            latencies = list(self.chat_latency_ms)
            uptime = round(time.time() - self._started_at, 1)

            chat_block = {
                "total": self.chat_total,
                "by_intent": dict(self.chat_by_intent),
                "by_v3_engine": dict(self.chat_by_v3_engine),
                "latency_ms": {
                    "p50": round(_percentile(latencies, 0.5), 1),
                    "p90": round(_percentile(latencies, 0.9), 1),
                    "max": round(max(latencies), 1) if latencies else 0.0,
                    "samples": len(latencies),
                },
                "answer_tokens_total": self.chat_answer_tokens_total,
                "retrieve_hits_avg": (
                    round(self.chat_retrieve_hits_sum / self.chat_retrieve_hits_count, 2)
                    if self.chat_retrieve_hits_count > 0
                    else 0.0
                ),
            }

            rag_block = {
                "qdrant_search_total": self.qdrant_search_total,
                "qdrant_search_success": self.qdrant_search_success,
                "qdrant_fallback_open_total": self.qdrant_fallback_open_total,
                "qdrant_error_total": self.qdrant_error_total,
            }

            emb_block = {
                "calls_total": self.embedding_calls_total,
                "retries_total": self.embedding_retries_total,
                "errors_total": self.embedding_errors_total,
            }

            hit_k_block = {
                "window_size": len(self._hit_k_window),
                "total_samples": self._hit_k_total_samples,
                "hit@1": self._hit_at_k(1),
                "hit@3": self._hit_at_k(3),
                "hit@5": self._hit_at_k(5),
                "hit@10": self._hit_at_k(10),
            }

            behavior_block = {
                "alerts_total": self.behavior_alerts_total,
                "alerts_by_type": dict(self.behavior_alerts_by_type),
            }

            rewrite_block = {
                "total": self.rewrite_total,
                "by_reason": dict(self.rewrite_by_reason),
            }

            rewrite_multi_block = {
                "total": self.rewrite_multi_total,
                "by_reason": dict(self.rewrite_multi_by_reason),
            }

            # M14：Resolver 决策质量指标（4 类）
            show_picker_n = self.resolver_by_action.get("show_picker", 0)
            ask_zero_n = self.resolver_by_action.get("ask_login_or_list", 0)
            m14_block = {
                "resolver_total": self.resolver_total,
                "by_action": dict(self.resolver_by_action),
                "by_total_orders": {
                    "zero": self.resolver_total_orders_zero,
                    "one": self.resolver_total_orders_one,
                    "many": self.resolver_total_orders_many,
                },
                "card_expected": self.resolver_card_expected,
                "card_sent_when_expected": self.resolver_card_sent_when_expected,
                "ratios": {
                    # N>=2 时返 SHOW_PICKER 的比例
                    "multi_order_disambiguation_accuracy": (
                        round(show_picker_n / self.resolver_total_orders_many, 4)
                        if self.resolver_total_orders_many > 0 else 0.0
                    ),
                    # N>=2 时返 SHOW_PICKER 的比例（同上，proactive_list_order 是别名）
                    "proactive_list_order_accuracy": (
                        round(show_picker_n / self.resolver_total_orders_many, 4)
                        if self.resolver_total_orders_many > 0 else 0.0
                    ),
                    # 0 订单场景返 ASK_LOGIN_OR_LIST 而非误答的比例
                    "no_order_no_completion_rate": (
                        round(ask_zero_n / self.resolver_total_orders_zero, 4)
                        if self.resolver_total_orders_zero > 0 else 0.0
                    ),
                    # SSE meta.card 应发且实际发的比例（分子仅含 expected=True ∩ sent=True）
                    "card_triggered_when_expected_rate": (
                        round(
                            self.resolver_card_sent_when_expected
                            / self.resolver_card_expected,
                            4,
                        )
                        if self.resolver_card_expected > 0 else 0.0
                    ),
                },
            }

            cb_block = circuit_breaker_stats or {}

            return {
                "uptime_seconds": uptime,
                "chat": chat_block,
                "rag": rag_block,
                "embedding": emb_block,
                "circuit_breaker": cb_block,
                "hit_at_k": hit_k_block,
                "behavior": behavior_block,
                "rewrite": rewrite_block,
                "rewrite_multi": rewrite_multi_block,
                "m14_resolver": m14_block,
            }


# =============================================================
# 全局单例
# =============================================================
metrics = Metrics()