"""P4-2 admin 运营聚合：纯函数 + SQLite 集成测试。"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import admin_analytics
from app.api.deps import require_admin
from app.models.base import Base
from app.models.message import Message
from app.models.operation_log import OperationLog
from app.models.user import User
from app.schemas.admin_analytics import AdminAnalyticsResponse
from app.services import analytics_service
from app.services.analytics_service import AnalyticsService


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl


@pytest.fixture
def db():
    from app.models import (
        conversation, knowledge_document, message, operation_log,
        order, product, refund, user, user_profile,
    )

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def admin_user():
    return User(
        id=1,
        username="admin",
        password_hash="fake",
        display_name="管理员",
        email="admin@example.com",
        phone="13800000000",
        role="admin",
        status=1,
    )


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(analytics_service, "redis_get", lambda: fake_redis)
    monkeypatch.setattr(admin_analytics, "try_log_action", lambda **kwargs: None)
    return fake_redis


def _add_message(db, *, session_id, user_id, role, created_at, latency_ms=None):
    db.add(Message(
        session_id=session_id,
        user_id=user_id,
        role=role,
        content=f"{role}-{session_id}",
        latency_ms=latency_ms,
        create_time=created_at,
    ))


def test_percentile_uses_linear_interpolation():
    samples = [100, 200, 300, 400]
    assert AnalyticsService.calculate_percentile(samples, 0.50) == 250.0
    assert AnalyticsService.calculate_percentile(samples, 0.95) == 385.0
    assert AnalyticsService.calculate_percentile([], 0.95) == 0.0


def test_daily_activity_groups_distinct_sessions_and_fills_empty_days(db):
    start = datetime(2026, 7, 10)
    end = datetime(2026, 7, 13)
    _add_message(db, session_id="s1", user_id=1, role="user", created_at=start + timedelta(hours=1))
    _add_message(db, session_id="s1", user_id=1, role="assistant", created_at=start + timedelta(hours=1, minutes=1))
    _add_message(db, session_id="s2", user_id=1, role="user", created_at=start + timedelta(hours=2))
    _add_message(db, session_id="s3", user_id=2, role="user", created_at=start + timedelta(days=2, hours=1))
    db.commit()

    points = AnalyticsService.get_daily_activity(db, start, end)

    assert points == [
        {"date": "2026-07-10", "conversations": 2, "active_users": 1, "messages": 3},
        {"date": "2026-07-11", "conversations": 0, "active_users": 0, "messages": 0},
        {"date": "2026-07-12", "conversations": 1, "active_users": 1, "messages": 1},
    ]


def test_latency_summary_only_counts_assistant_messages_in_window(db):
    start = datetime(2026, 7, 10)
    end = start + timedelta(days=1)
    for index, latency in enumerate([100, 200, 300, 400]):
        _add_message(
            db,
            session_id=f"s{index}",
            user_id=1,
            role="assistant",
            created_at=start + timedelta(hours=index + 1),
            latency_ms=latency,
        )
    _add_message(db, session_id="user", user_id=1, role="user", created_at=start, latency_ms=9999)
    _add_message(db, session_id="old", user_id=1, role="assistant", created_at=start - timedelta(days=1), latency_ms=9999)
    db.commit()

    result = AnalyticsService.get_latency_summary(db, start, end)

    assert result == {"samples": 4, "p50_ms": 250.0, "p95_ms": 385.0}


def test_handoff_summary_groups_priority_and_category(db):
    start = datetime(2026, 7, 10)
    end = start + timedelta(days=1)
    db.add_all([
        OperationLog(
            action="chat_handoff_p0",
            target_type="session",
            target_id="s1",
            detail={"priority": "P0", "category": "complaint"},
            create_time=start + timedelta(hours=1),
        ),
        OperationLog(
            action="chat_handoff_user_requested",
            target_type="session",
            target_id="s2",
            detail={"handoff_id": "h2"},
            create_time=start + timedelta(hours=2),
        ),
        OperationLog(
            action="chat_handoff_business_rule",
            target_type="session",
            target_id="s3",
            detail={"priority": "P1", "category": "quality"},
            create_time=start + timedelta(hours=3),
        ),
        OperationLog(
            action="chat",
            target_type="session",
            target_id="ignored",
            detail={},
            create_time=start + timedelta(hours=4),
        ),
    ])
    db.commit()

    result = AnalyticsService.get_handoff_summary(db, start, end)

    assert result["total"] == 3
    assert result["by_priority"] == {"P0": 1, "P1": 1, "P2": 1, "unclassified": 0}
    assert result["by_category"] == {"complaint": 1, "user_requested": 1, "quality": 1}
    assert result["coverage_complete"] is False


def test_hit_at_k_summary_maps_metrics_keys(monkeypatch):
    monkeypatch.setattr(
        analytics_service.metrics,
        "snapshot",
        lambda: {
            "hit_at_k": {
                "window_size": 20,
                "total_samples": 35,
                "hit@1": 0.5,
                "hit@3": 0.7,
                "hit@5": 0.8,
                "hit@10": 0.9,
            }
        },
    )

    assert AnalyticsService.get_hit_at_k_summary() == {
        "window_size": 20,
        "total_samples": 35,
        "hit_at_1": 0.5,
        "hit_at_3": 0.7,
        "hit_at_5": 0.8,
        "hit_at_10": 0.9,
    }


def test_get_analytics_reads_cached_payload_without_querying_db(_patch_external):
    start = datetime(2026, 7, 10)
    end = start + timedelta(days=7)
    cache_key = AnalyticsService._cache_key(start, end)
    _patch_external.values[cache_key] = json.dumps({"marker": "cached", "cache_hit": False})

    class _FailingDb:
        def execute(self, _statement):
            raise AssertionError("cache hit should not query DB")

    result = AnalyticsService.get_analytics(_FailingDb(), start, end)

    assert result == {"marker": "cached", "cache_hit": True}


def test_router_requires_admin_dependency():
    route = next(
        item
        for item in admin_analytics.router.routes
        if item.path == "/api/admin/analytics"
    )
    assert any(dependency.call is require_admin for dependency in route.dependant.dependencies)


def test_endpoint_requires_time_window(admin_user, db):
    with pytest.raises(HTTPException) as exc_info:
        admin_analytics.get_admin_analytics(
            request=_FakeRequest(),
            start_date=None,
            end_date=datetime(2026, 7, 19),
            admin=admin_user,
            db=db,
        )
    assert exc_info.value.status_code == 400


def test_endpoint_rejects_window_over_90_days(admin_user, db):
    with pytest.raises(HTTPException) as exc_info:
        admin_analytics.get_admin_analytics(
            request=_FakeRequest(),
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 7, 19),
            admin=admin_user,
            db=db,
        )
    assert exc_info.value.status_code == 400


def test_endpoint_returns_schema_valid_aggregate(monkeypatch, admin_user, db):
    start = datetime(2026, 7, 10)
    end = start + timedelta(days=2)
    _add_message(db, session_id="s1", user_id=2, role="user", created_at=start + timedelta(hours=1))
    _add_message(
        db,
        session_id="s1",
        user_id=2,
        role="assistant",
        created_at=start + timedelta(hours=1, minutes=1),
        latency_ms=850,
    )
    db.add(OperationLog(
        action="chat_handoff_p0",
        target_type="session",
        target_id="s1",
        detail={"priority": "P0", "category": "complaint"},
        create_time=start + timedelta(hours=2),
    ))
    db.commit()
    monkeypatch.setattr(
        analytics_service.metrics,
        "snapshot",
        lambda: {"hit_at_k": {"window_size": 1, "total_samples": 1, "hit@1": 1.0}},
    )

    result = admin_analytics.get_admin_analytics(
        request=_FakeRequest(),
        start_date=start,
        end_date=end,
        admin=admin_user,
        db=db,
    )
    parsed = AdminAnalyticsResponse.model_validate(result)

    assert parsed.daily_activity[0].conversations == 1
    assert parsed.latency.p95_ms == 850.0
    assert parsed.handoffs.by_priority["P0"] == 1
    assert parsed.hit_at_k.hit_at_1 == 1.0
    assert len(parsed.limitations) == 2


class _FakeRequest:
    headers = {}

    @property
    def client(self):
        return type("C", (), {"host": "testclient"})()
