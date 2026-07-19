"""
test_admin_conversations.py — P4-1 L1 mock + L2 集成测试：admin 全局会话查询

按 SOP-V1 §2.2 数据可信验证：
- L1 mock：直接调 endpoint 函数（绕开 FastAPI Depends + With_safe_session MySQL 链路）
- L2 集成：用 db_session 风格 SQLite engine，patch audit_service 避免 MySQL 链接

测试目标：
- RBAC：非 admin 返 403（require_admin 在 deps 中已验，这里测 override 路径）
- 强制时间窗：无 start_date / end_date 返 400
- 时间跨度 ≤ 90 天，否则 400
- 邮箱脱敏：admin 视图 email 形如 a***@b.com
- cursor 分页：has_more / next_cursor 正确
- messages 接口：admin 可读任意 session_id（含 RAG contexts）
"""
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import admin_conversations
from app.models.base import Base
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User


# =============================================================
# Fixtures
# =============================================================
@pytest.fixture
def sqlite_engine():
    """每个测试函数独立 SQLite in-memory（StaticPool 单连接共享表）

    StaticPool 关键：
    - SQLite in-memory 默认每个连接独立 schema
    - StaticPool 让所有 session 共用同一 connection → 表存在所有 session 可见
    """
    from app.models import (
        user, conversation, message, knowledge_document,
        operation_log, order, refund, product, user_profile,
    )
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
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


@pytest.fixture
def normal_user():
    return User(
        id=2,
        username="alice",
        password_hash="fake",
        display_name="Alice",
        email="alice@example.com",
        phone="13900000000",
        role="user",
        status=1,
    )


@pytest.fixture
def db_session_factory(sqlite_engine):
    """返回 (db, close) — 测试结束时关闭"""
    SessionLocal = sessionmaker(bind=sqlite_engine)
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _patch_audit(monkeypatch):
    """全局 mock try_log_action（避免 with_safe_session 连 MySQL）

    审计是 best-effort，单元测试不验证 audit 写入，
    只验证 endpoint 内部逻辑。P4-2 加 analytics 测试时单独验 audit。
    """
    monkeypatch.setattr(
        admin_conversations, "try_log_action",
        lambda **kwargs: None,  # no-op
    )


def _seed_conversation_seedless(db, user_id, days_ago=1, session_id="test-session-1",
                               title="测试会话", message_count=5, first_query="怎么退货"):
    """造一条会话（**不**自动造 User — 适用于已显式 _seed_user 的场景）"""
    conv = Conversation(
        session_id=session_id,
        user_id=user_id,
        title=title,
        status=1,
        message_count=message_count,
        first_query=first_query,
        last_message_at=datetime.utcnow() - timedelta(days=days_ago),
        create_time=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(conv)
    db.commit()


def _seed_user(db, user_id, username, email, role="user"):
    """造一个 User（如不存在）"""
    existing = db.query(User).filter(User.id == user_id).first()
    if existing:
        return existing
    user = User(
        id=user_id,
        username=username,
        password_hash="fake",
        display_name=username,
        email=email,
        phone=f"138{user_id:08d}",
        role=role,
        status=1,
    )
    db.add(user)
    db.commit()
    return user


def _seed_conversation(db, user_id, days_ago=1, session_id="test-session-1",
                      title="测试会话", message_count=5, first_query="怎么退货"):
    """造一条会话（自动按需造关联 User）"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        _seed_user(db, user_id=user_id, username=f"user_{user_id}", email=f"u{user_id}@x.com")

    conv = Conversation(
        session_id=session_id,
        user_id=user_id,
        title=title,
        status=1,
        message_count=message_count,
        first_query=first_query,
        last_message_at=datetime.utcnow() - timedelta(days=days_ago),
        create_time=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(conv)
    db.commit()


# =============================================================
# L1 单元：脱敏 + 时间窗 + cursor 校验（纯函数测试）
# =============================================================
def test_email_masking_helper():
    """_mask_email 工具函数：a***@b.com 形式"""
    assert admin_conversations._mask_email("alice@example.com") == "a***@example.com"
    assert admin_conversations._mask_email("bob@example.com") == "b***@example.com"
    # 单字符 local
    assert admin_conversations._mask_email("a@example.com") == "a***@example.com"
    # 无 @ 视为无效，原样返回
    assert admin_conversations._mask_email("invalid-email") == "invalid-email"
    # None / 空字符串
    assert admin_conversations._mask_email(None) is None
    assert admin_conversations._mask_email("") == ""


def test_missing_time_window_raises_400(sqlite_engine, admin_user, db_session_factory):
    """无 start_date 或 end_date → 400（防全表扫）"""
    # 缺 end_date
    with pytest.raises(HTTPException) as exc_info:
        admin_conversations.list_admin_conversations(
            request=_FakeRequest(),
            start_date=datetime.utcnow(),
            end_date=None,
            user_id=None,
            keyword=None,
            has_handoff=None,
            min_latency_ms=None,
            limit=20,
            cursor=None,
            admin=admin_user,
            db=db_session_factory,
        )
    assert exc_info.value.status_code == 400
    assert "start_date + end_date" in str(exc_info.value.detail)

    # 缺 start_date
    with pytest.raises(HTTPException) as exc_info:
        admin_conversations.list_admin_conversations(
            request=_FakeRequest(),
            start_date=None,
            end_date=datetime.utcnow(),
            user_id=None,
            keyword=None,
            has_handoff=None,
            min_latency_ms=None,
            limit=20,
            cursor=None,
            admin=admin_user,
            db=db_session_factory,
        )
    assert exc_info.value.status_code == 400


def test_end_date_must_be_after_start_date(sqlite_engine, admin_user, db_session_factory):
    """end_date <= start_date → 400"""
    now = datetime.utcnow()
    with pytest.raises(HTTPException) as exc_info:
        admin_conversations.list_admin_conversations(
            request=_FakeRequest(),
            start_date=now,
            end_date=now,
            user_id=None,
            keyword=None,
            has_handoff=None,
            min_latency_ms=None,
            limit=20,
            cursor=None,
            admin=admin_user,
            db=db_session_factory,
        )
    assert exc_info.value.status_code == 400


def test_window_exceeds_90_days_returns_400(sqlite_engine, admin_user, db_session_factory):
    """时间范围 > 90 天 → 400（防大跨度扫描）"""
    start = datetime.utcnow() - timedelta(days=100)
    end = datetime.utcnow()
    with pytest.raises(HTTPException) as exc_info:
        admin_conversations.list_admin_conversations(
            request=_FakeRequest(),
            start_date=start,
            end_date=end,
            user_id=None,
            keyword=None,
            has_handoff=None,
            min_latency_ms=None,
            limit=20,
            cursor=None,
            admin=admin_user,
            db=db_session_factory,
        )
    assert exc_info.value.status_code == 400
    assert "≤ 90 天" in str(exc_info.value.detail)


def test_cursor_invalid_format_raises_400(sqlite_engine, admin_user, db_session_factory):
    """cursor 非 ISO8601 格式 → 400"""
    with pytest.raises(HTTPException) as exc_info:
        admin_conversations.list_admin_conversations(
            request=_FakeRequest(),
            start_date=datetime.utcnow() - timedelta(days=7),
            end_date=datetime.utcnow(),
            user_id=None,
            keyword=None,
            has_handoff=None,
            min_latency_ms=None,
            limit=20,
            cursor="not-a-date",
            admin=admin_user,
            db=db_session_factory,
        )
    assert exc_info.value.status_code == 400
    assert "cursor 格式错误" in str(exc_info.value.detail)


# =============================================================
# L2 集成：用 SQLite 真查询验证 ORM + 业务逻辑
# =============================================================
def test_l2_list_returns_conversations_in_window(sqlite_engine, admin_user, normal_user, db_session_factory):
    """验证：3 条不同天数的会话，时间窗内正确返回"""
    # seed User 才能 JOIN（默认 helper 会自动 seed，但这里显式更清晰）
    _seed_user(db_session_factory, user_id=admin_user.id, username="admin", email="admin@x.com")
    _seed_user(db_session_factory, user_id=normal_user.id, username="alice", email="alice@x.com")
    _seed_conversation_seedless(db_session_factory, user_id=admin_user.id, days_ago=1, session_id="admin-s1")
    _seed_conversation_seedless(db_session_factory, user_id=normal_user.id, days_ago=3, session_id="user-s1")
    _seed_conversation_seedless(db_session_factory, user_id=normal_user.id, days_ago=100, session_id="user-old")

    result = admin_conversations.list_admin_conversations(
        request=_FakeRequest(),
        start_date=datetime.utcnow() - timedelta(days=7),
        end_date=datetime.utcnow(),
        user_id=None,
        keyword=None,
        has_handoff=None,
        min_latency_ms=None,
        limit=20,
        cursor=None,
        admin=admin_user,
        db=db_session_factory,
    )
    assert result.total == 2
    session_ids = {c.session_id for c in result.conversations}
    assert session_ids == {"admin-s1", "user-s1"}


def test_l2_filter_by_user_id(sqlite_engine, admin_user, normal_user, db_session_factory):
    """user_id 过滤生效"""
    # 先 seed normal_user 到 DB（用 fixture 的 email/username）
    _seed_user(db_session_factory, user_id=normal_user.id, username="alice", email="alice@example.com")
    _seed_conversation_seedless(db_session_factory, user_id=normal_user.id, days_ago=1, session_id="user-s1")

    result = admin_conversations.list_admin_conversations(
        request=_FakeRequest(),
        start_date=datetime.utcnow() - timedelta(days=7),
        end_date=datetime.utcnow(),
        user_id=normal_user.id,
        keyword=None,
        has_handoff=None,
        min_latency_ms=None,
        limit=20,
        cursor=None,
        admin=admin_user,
        db=db_session_factory,
    )
    assert result.total == 1
    assert result.conversations[0].session_id == "user-s1"
    assert result.conversations[0].username == "alice"


def test_l2_keyword_filter(sqlite_engine, admin_user, db_session_factory):
    """keyword LIKE 过滤生效"""
    _seed_user(db_session_factory, user_id=admin_user.id, username="admin", email="admin@x.com")
    _seed_conversation_seedless(db_session_factory, user_id=admin_user.id, days_ago=1, session_id="s-refund", first_query="怎么退货")
    _seed_conversation_seedless(db_session_factory, user_id=admin_user.id, days_ago=1, session_id="s-shipping", first_query="运费险怎么买")

    result = admin_conversations.list_admin_conversations(
        request=_FakeRequest(),
        start_date=datetime.utcnow() - timedelta(days=7),
        end_date=datetime.utcnow(),
        user_id=None,
        keyword="退货",
        has_handoff=None,
        min_latency_ms=None,
        limit=20,
        cursor=None,
        admin=admin_user,
        db=db_session_factory,
    )
    assert result.total == 1
    assert result.conversations[0].session_id == "s-refund"


def test_l2_email_is_masked_in_admin_view(sqlite_engine, admin_user, normal_user, db_session_factory):
    """admin 视图：email 字段脱敏为 a***@b.com 形式"""
    # 必须先 seed 用户（fixture 里给的 email = alice@example.com）
    _seed_user(db_session_factory, user_id=normal_user.id, username="alice", email="alice@example.com")
    _seed_conversation_seedless(db_session_factory, user_id=normal_user.id, days_ago=1, session_id="user-s1")

    result = admin_conversations.list_admin_conversations(
        request=_FakeRequest(),
        start_date=datetime.utcnow() - timedelta(days=7),
        end_date=datetime.utcnow(),
        user_id=None,
        keyword=None,
        has_handoff=None,
        min_latency_ms=None,
        limit=20,
        cursor=None,
        admin=admin_user,
        db=db_session_factory,
    )
    item = result.conversations[0]
    # 邮箱脱敏（alice@example.com → a***@example.com）
    assert item.user_email_masked == "a***@example.com"


def test_l2_cursor_pagination(sqlite_engine, admin_user, db_session_factory):
    """5 条会话分页：has_more + next_cursor 工作正确"""
    _seed_user(db_session_factory, user_id=admin_user.id, username="admin", email="admin@x.com")
    for i in range(5):
        _seed_conversation_seedless(
            db_session_factory,
            user_id=admin_user.id,
            days_ago=i,
            session_id=f"s-page-{i}",
        )

    # 第 1 页：limit=2
    page1 = admin_conversations.list_admin_conversations(
        request=_FakeRequest(),
        start_date=datetime.utcnow() - timedelta(days=10),
        end_date=datetime.utcnow(),
        user_id=None,
        keyword=None,
        has_handoff=None,
        min_latency_ms=None,
        limit=2,
        cursor=None,
        admin=admin_user,
        db=db_session_factory,
    )
    assert page1.total == 2
    assert page1.has_more is True
    assert page1.next_cursor is not None

    # 第 2 页：用 page1.next_cursor
    page2 = admin_conversations.list_admin_conversations(
        request=_FakeRequest(),
        start_date=datetime.utcnow() - timedelta(days=10),
        end_date=datetime.utcnow(),
        user_id=None,
        keyword=None,
        has_handoff=None,
        min_latency_ms=None,
        limit=2,
        cursor=page1.next_cursor,
        admin=admin_user,
        db=db_session_factory,
    )
    assert page2.total <= 2
    page1_ids = {c.session_id for c in page1.conversations}
    page2_ids = {c.session_id for c in page2.conversations}
    assert page1_ids.isdisjoint(page2_ids)


def test_l2_get_messages_returns_with_contexts(sqlite_engine, admin_user, normal_user, db_session_factory):
    """admin 可读任意 session_id + RAG contexts 完整返回"""
    _seed_user(db_session_factory, user_id=normal_user.id, username="alice", email="alice@x.com")
    _seed_conversation_seedless(db_session_factory, user_id=normal_user.id, days_ago=1, session_id="user-s1")

    # 加 2 条 message（含 RAG contexts）
    db_session_factory.add(Message(
        session_id="user-s1",
        user_id=normal_user.id,
        role="user",
        content="怎么退货",
        token_count=5,
        latency_ms=200,
    ))
    db_session_factory.add(Message(
        session_id="user-s1",
        user_id=normal_user.id,
        role="assistant",
        content="7天无理由退货",
        token_count=120,
        latency_ms=850,
        contexts=[{"id": "P1", "text": "7天无理由退货政策", "score": 0.92}],
        scores=[0.92],
    ))
    db_session_factory.commit()

    result = admin_conversations.get_admin_messages(
        request=_FakeRequest(),
        session_id="user-s1",
        limit=50,
        cursor=None,
        admin=admin_user,
        db=db_session_factory,
    )
    assert result.session_id == "user-s1"
    assert result.user_id == normal_user.id
    assert len(result.messages) == 2
    # 倒序：assistant 在前
    assistant_msg = result.messages[0]
    assert assistant_msg.role == "assistant"
    assert assistant_msg.contexts == [{"id": "P1", "text": "7天无理由退货政策", "score": 0.92}]
    assert assistant_msg.scores == [0.92]
    assert assistant_msg.latency_ms == 850


def test_l2_get_messages_session_not_found_returns_404(sqlite_engine, admin_user, db_session_factory):
    """不存在的 session_id → 404"""
    with pytest.raises(HTTPException) as exc_info:
        admin_conversations.get_admin_messages(
            request=_FakeRequest(),
            session_id="nonexistent-session",
            limit=50,
            cursor=None,
            admin=admin_user,
            db=db_session_factory,
        )
    assert exc_info.value.status_code == 404


# =============================================================
# Helpers
# =============================================================
class _FakeRequest:
    """FastAPI Request 的最小替身（避免 import 整个 starlette Request）"""
    headers: dict = {}

    @property
    def client(self):
        return type("C", (), {"host": "testclient"})()