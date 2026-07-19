"""
admin analytics 运营聚合响应模型（P4-2）。

仅返回聚合结果，不暴露用户 PII 或消息原文。
"""
from datetime import date, datetime
from typing import Dict, List

from pydantic import BaseModel, Field


class DailyActivityPoint(BaseModel):
    """单日会话活跃度。"""

    date: date
    conversations: int = Field(..., ge=0, description="当日有消息的去重会话数")
    active_users: int = Field(..., ge=0, description="当日有消息的去重用户数")
    messages: int = Field(..., ge=0, description="当日消息总数")


class LatencySummary(BaseModel):
    """时间窗内 assistant 消息响应延迟。"""

    samples: int = Field(..., ge=0)
    p50_ms: float = Field(..., ge=0)
    p95_ms: float = Field(..., ge=0)


class HandoffSummary(BaseModel):
    """持久化审计日志中的转人工事件分布。"""

    total: int = Field(..., ge=0)
    by_priority: Dict[str, int]
    by_category: Dict[str, int]
    coverage_complete: bool = Field(
        ...,
        description="False 表示当前自动 P1 升级尚未全部写入 operation_logs",
    )
    data_source: str = Field(..., description="聚合数据源")


class HitAtKSummary(BaseModel):
    """进程内最近 100 次 RAG 检索的 hit@K 快照。"""

    window_size: int = Field(..., ge=0)
    total_samples: int = Field(..., ge=0)
    hit_at_1: float = Field(..., ge=0, le=1)
    hit_at_3: float = Field(..., ge=0, le=1)
    hit_at_5: float = Field(..., ge=0, le=1)
    hit_at_10: float = Field(..., ge=0, le=1)


class AdminAnalyticsResponse(BaseModel):
    """admin 运营驾驶舱聚合响应。"""

    start_date: datetime
    end_date: datetime
    generated_at: datetime
    cache_hit: bool
    cache_ttl_seconds: int = Field(..., ge=0)
    daily_activity: List[DailyActivityPoint]
    latency: LatencySummary
    handoffs: HandoffSummary
    hit_at_k: HitAtKSummary
    limitations: List[str] = Field(default_factory=list)
