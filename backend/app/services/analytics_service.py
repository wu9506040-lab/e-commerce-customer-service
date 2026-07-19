"""
admin 运营聚合服务（P4-2）。

数据口径：
- 日活：messages 表按 create_time 聚合去重 session/user。
- 延迟：assistant 消息 latency_ms 的 P50/P95。
- 转人工：operation_logs 中 chat_handoff* 审计事件。
- hit@K：进程内 Metrics 最近 100 次检索窗口。

Redis 缓存为 best-effort；连接失败时直接返回数据库实时结果。
"""
import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Dict, List

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.redis_client import get_client as redis_get
from app.models.message import Message
from app.models.operation_log import OperationLog
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

ANALYTICS_CACHE_PREFIX = "admin:analytics:v1:"
ANALYTICS_CACHE_TTL_SECONDS = 300


class AnalyticsService:
    """只读运营聚合服务。"""

    @staticmethod
    def calculate_percentile(samples: List[float], percentile: float) -> float:
        """按线性插值计算分位数，与 Metrics 现有口径一致。"""
        if not samples:
            return 0.0
        ordered = sorted(samples)
        position = (len(ordered) - 1) * percentile
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        if lower == upper:
            return float(ordered[lower])
        value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
        return float(value)

    @staticmethod
    def get_daily_activity(
        db: Session,
        start_date: datetime,
        end_date: datetime,
    ) -> List[Dict]:
        """返回时间窗内按日聚合的会话、用户和消息数，缺失日期补零。"""
        day_expr = func.date(Message.create_time)
        rows = db.execute(
            select(
                day_expr.label("day"),
                func.count(func.distinct(Message.session_id)).label("conversations"),
                func.count(func.distinct(Message.user_id)).label("active_users"),
                func.count(Message.id).label("messages"),
            )
            .where(
                Message.deleted == 0,
                Message.create_time >= start_date,
                Message.create_time < end_date,
            )
            .group_by(day_expr)
            .order_by(day_expr)
        ).all()

        values: Dict[str, Dict] = {}
        for row in rows:
            day_value = row.day.isoformat() if isinstance(row.day, date) else str(row.day)
            values[day_value] = {
                "date": day_value,
                "conversations": int(row.conversations or 0),
                "active_users": int(row.active_users or 0),
                "messages": int(row.messages or 0),
            }

        points: List[Dict] = []
        current_day = start_date.date()
        while datetime.combine(current_day, time.min) < end_date:
            key = current_day.isoformat()
            points.append(values.get(key, {
                "date": key,
                "conversations": 0,
                "active_users": 0,
                "messages": 0,
            }))
            current_day += timedelta(days=1)
        return points

    @staticmethod
    def get_latency_summary(
        db: Session,
        start_date: datetime,
        end_date: datetime,
    ) -> Dict:
        """计算时间窗内 assistant 消息 latency_ms 的 P50/P95。"""
        samples = [
            float(value)
            for value in db.execute(
                select(Message.latency_ms).where(
                    Message.deleted == 0,
                    Message.role == "assistant",
                    Message.latency_ms.is_not(None),
                    Message.latency_ms >= 0,
                    Message.create_time >= start_date,
                    Message.create_time < end_date,
                )
            ).scalars().all()
        ]
        return {
            "samples": len(samples),
            "p50_ms": round(AnalyticsService.calculate_percentile(samples, 0.50), 1),
            "p95_ms": round(AnalyticsService.calculate_percentile(samples, 0.95), 1),
        }

    @staticmethod
    def get_handoff_summary(
        db: Session,
        start_date: datetime,
        end_date: datetime,
    ) -> Dict:
        """聚合 chat_handoff* 审计事件；未知优先级单独计数。"""
        rows = db.execute(
            select(OperationLog.action, OperationLog.detail).where(
                OperationLog.deleted == 0,
                OperationLog.action.like("chat_handoff%"),
                OperationLog.create_time >= start_date,
                OperationLog.create_time < end_date,
            )
        ).all()

        by_priority = {"P0": 0, "P1": 0, "P2": 0, "unclassified": 0}
        by_category: Dict[str, int] = {}

        for action, raw_detail in rows:
            detail = raw_detail if isinstance(raw_detail, dict) else {}
            priority = str(detail.get("priority") or "").upper()
            if not priority and action == "chat_handoff_p0":
                priority = "P0"
            elif not priority and action == "chat_handoff_user_requested":
                priority = "P2"
            if priority not in ("P0", "P1", "P2"):
                priority = "unclassified"
            by_priority[priority] += 1

            category = detail.get("category") or detail.get("detected_category")
            if not category and action == "chat_handoff_user_requested":
                category = "user_requested"
            category_key = str(category or "unclassified")
            by_category[category_key] = by_category.get(category_key, 0) + 1

        return {
            "total": len(rows),
            "by_priority": by_priority,
            "by_category": by_category,
            "coverage_complete": False,
            "data_source": "operation_logs:chat_handoff*",
        }

    @staticmethod
    def get_hit_at_k_summary() -> Dict:
        """读取 Metrics 单例最近 100 次 RAG 检索窗口。"""
        snapshot = metrics.snapshot().get("hit_at_k", {})
        return {
            "window_size": int(snapshot.get("window_size", 0)),
            "total_samples": int(snapshot.get("total_samples", 0)),
            "hit_at_1": float(snapshot.get("hit@1", 0.0)),
            "hit_at_3": float(snapshot.get("hit@3", 0.0)),
            "hit_at_5": float(snapshot.get("hit@5", 0.0)),
            "hit_at_10": float(snapshot.get("hit@10", 0.0)),
        }

    @staticmethod
    def _cache_key(start_date: datetime, end_date: datetime) -> str:
        raw = f"{start_date.isoformat()}|{end_date.isoformat()}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"{ANALYTICS_CACHE_PREFIX}{digest}"

    @staticmethod
    def _load_cache(cache_key: str) -> Dict | None:
        try:
            raw = redis_get().get(cache_key)
            if not raw:
                return None
            value = json.loads(raw)
            if not isinstance(value, dict):
                return None
            value["cache_hit"] = True
            return value
        except Exception as exc:
            logger.warning(f"admin analytics Redis 读取失败，回退数据库: {exc}")
            return None

    @staticmethod
    def _save_cache(cache_key: str, payload: Dict) -> None:
        try:
            redis_get().setex(
                cache_key,
                ANALYTICS_CACHE_TTL_SECONDS,
                json.dumps(payload, ensure_ascii=False, default=str),
            )
        except Exception as exc:
            logger.warning(f"admin analytics Redis 写入失败，忽略缓存: {exc}")

    @staticmethod
    def get_analytics(
        db: Session,
        start_date: datetime,
        end_date: datetime,
    ) -> Dict:
        """读取缓存或计算完整 admin analytics 响应。"""
        cache_key = AnalyticsService._cache_key(start_date, end_date)
        cached = AnalyticsService._load_cache(cache_key)
        if cached is not None:
            return cached

        payload = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "generated_at": datetime.utcnow().isoformat(),
            "cache_hit": False,
            "cache_ttl_seconds": ANALYTICS_CACHE_TTL_SECONDS,
            "daily_activity": AnalyticsService.get_daily_activity(db, start_date, end_date),
            "latency": AnalyticsService.get_latency_summary(db, start_date, end_date),
            "handoffs": AnalyticsService.get_handoff_summary(db, start_date, end_date),
            "hit_at_k": AnalyticsService.get_hit_at_k_summary(),
            "limitations": [
                "hit@K 为当前进程最近 100 次检索窗口，服务重启后清零",
                "当前 operation_logs 未完整持久化 RefundFlow 自动 P1 升级，handoff 分布为部分口径",
            ],
        }
        AnalyticsService._save_cache(cache_key, payload)
        return payload
