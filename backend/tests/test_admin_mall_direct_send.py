"""test_admin_mall_direct_send.py — V19.1 商城渠道人工直发 + human 标记透传

按 SOP-V1 §2.2「数据可信验证」：L1 mock 直接调 endpoint 函数（绕开 FastAPI Depends
与 MySQL 链路），SQLite in-memory + StaticPool 落库，断言**落库事实**而非函数返回值。

覆盖目标：
1. mall 渠道：cid 直接作 session_id（禁止加 xianyu- 前缀）
2. mall 渠道：落库 role=assistant + intent_snapshot={human:True, agent:用户名}；queued=False
3. mall 渠道：不下发 daemon 命令（商城买家端靠轮询拉取）
4. xianyu 渠道（默认）：cid 加 xianyu- 前缀；queued=True 且向 daemon 频道 publish
5. get_messages：intent_snapshot.human=True → human=True；普通消息 / 非 dict → human=False
"""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import conversations as conversations_api
from app.api import xianyu_admin
from app.models.base import Base
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User


# =============================================================
# Fixtures
# =============================================================
@pytest.fixture
def sqlite_engine():
    """每个测试函数独立 SQLite in-memory（StaticPool 单连接共享表）"""
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
def db_session(sqlite_engine):
    SessionLocal = sessionmaker(bind=sqlite_engine)
    db = SessionLocal()
    yield db
    db.close()


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
def buyer():
    return User(
        id=2,
        username="buyer",
        password_hash="fake",
        display_name="买家",
        email="buyer@example.com",
        phone="13900000000",
        role="user",
        status=1,
    )


@pytest.fixture
def fake_redis(monkeypatch):
    """mock daemon 下发通道（避免连真 Redis）"""
    fake = MagicMock()
    monkeypatch.setattr(xianyu_admin, "_redis", lambda: fake)
    return fake


def _seed_conversation(db, user_id, session_id):
    db.add(
        Conversation(
            session_id=session_id,
            user_id=user_id,
            title="商城会话",
            deleted=0,
        )
    )
    db.commit()


def _seed_message(db, session_id, user_id, role, content, intent_snapshot=None):
    db.add(
        Message(
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
            intent_snapshot=intent_snapshot,
            create_time=datetime.now(),
            deleted=0,
        )
    )
    db.commit()


# =============================================================
# 1. 商城渠道人工直发
# =============================================================
def test_mall_direct_send_uses_cid_as_session_id(db_session, admin_user):
    """mall：cid 即 session_id，禁止加 xianyu- 前缀"""
    body = xianyu_admin.SendBody(
        cid="mall-session-42", text="您好，已为您安排退款", channel="mall"
    )
    result = xianyu_admin.send_xianyu_message(
        body=body, db=db_session, admin=admin_user
    )

    assert result["ok"] is True
    assert result["queued"] is False
    assert result["channel"] == "mall"
    assert result["cid"] == "mall-session-42"

    rows = db_session.query(Message).filter(
        Message.session_id == "mall-session-42"
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert not row.session_id.startswith(xianyu_admin.SESSION_PREFIX)
    assert row.role == "assistant"
    assert row.content == "您好，已为您安排退款"
    assert row.user_id == admin_user.id
    assert row.intent_snapshot == {"human": True, "agent": "admin"}


def test_mall_direct_send_does_not_publish_daemon_command(
    db_session, admin_user, fake_redis
):
    """mall：商城买家端靠轮询拉取，不得向 daemon 频道下发"""
    body = xianyu_admin.SendBody(cid="mall-session-7", text="人工回复", channel="mall")
    xianyu_admin.send_xianyu_message(body=body, db=db_session, admin=admin_user)

    fake_redis.publish.assert_not_called()


def test_default_channel_is_xianyu_with_prefix_and_publish(
    db_session, admin_user, fake_redis
):
    """未显式传 channel → 闲鱼分支：加前缀 + daemon 下发"""
    body = xianyu_admin.SendBody(cid="abc123", text="闲鱼回复")
    assert body.channel == "xianyu"

    result = xianyu_admin.send_xianyu_message(
        body=body, db=db_session, admin=admin_user
    )

    assert result["queued"] is True
    rows = db_session.query(Message).filter(
        Message.session_id == "xianyu-abc123"
    ).all()
    assert len(rows) == 1
    assert rows[0].intent_snapshot == {"human": True, "agent": "admin"}
    fake_redis.publish.assert_called_once()


# =============================================================
# 2. human 标记透传
# =============================================================
def test_get_messages_flags_human_message(db_session, buyer):
    """intent_snapshot.human=True → human=True；普通消息 → human=False"""
    session_id = "mall-session-42"
    _seed_conversation(db_session, buyer.id, session_id)
    _seed_message(
        db_session, session_id, buyer.id, "user", "在吗", {"primary": "chitchat"}
    )
    _seed_message(
        db_session,
        session_id,
        buyer.id,
        "assistant",
        "您好，已为您安排退款",
        {"human": True, "agent": "admin"},
    )

    resp = conversations_api.get_messages(
        session_id=session_id,
        limit=20,
        cursor=None,
        current_user=buyer,
        db=db_session,
    )

    # 按 id 倒序：人工回复在第一条
    assert len(resp.messages) == 2
    assert resp.messages[0].human is True
    assert resp.messages[0].role == "assistant"
    assert resp.messages[1].human is False
    assert resp.messages[1].role == "user"


def test_get_messages_human_false_when_snapshot_is_none(db_session, buyer):
    """intent_snapshot 为 None → human=False（不抛异常）"""
    session_id = "mall-session-99"
    _seed_conversation(db_session, buyer.id, session_id)
    _seed_message(db_session, session_id, buyer.id, "assistant", "AI 回复", None)

    resp = conversations_api.get_messages(
        session_id=session_id,
        limit=20,
        cursor=None,
        current_user=buyer,
        db=db_session,
    )

    assert len(resp.messages) == 1
    assert resp.messages[0].human is False


def test_get_messages_human_false_when_flag_not_boolean_true(db_session, buyer):
    """intent_snapshot.human 为非布尔 True 值（'true'）→ 仍判为 False"""
    session_id = "mall-session-100"
    _seed_conversation(db_session, buyer.id, session_id)
    _seed_message(
        db_session, session_id, buyer.id, "assistant", "AI 回复", {"human": "true"}
    )

    resp = conversations_api.get_messages(
        session_id=session_id,
        limit=20,
        cursor=None,
        current_user=buyer,
        db=db_session,
    )

    assert resp.messages[0].human is False
