"""
V16.3.3 闲鱼独立表测试（profiles/conversations/items）

按 CLAUDE.md 调试铁律：SQLite in-memory
"""
import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models import xianyu_tables  # noqa - 注册


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


# ============================================================
# 1. XianyuBuyerProfile（7 天 TTL）
# ============================================================
class TestBuyerProfile:
    def test_create_profile(self, db_session):
        from app.models.xianyu_tables import XianyuBuyerProfile
        p = XianyuBuyerProfile(
            unb="u1", nick="zwyyy", is_returning_buyer=False
        )
        db_session.add(p)
        db_session.commit()
        assert p.id is not None
        assert p.unb == "u1"

    def test_query_by_unb(self, db_session):
        from app.models.xianyu_tables import XianyuBuyerProfile
        p = XianyuBuyerProfile(unb="u1", nick="zwyyy", is_returning_buyer=True)
        db_session.add(p)
        db_session.commit()
        found = db_session.query(XianyuBuyerProfile).filter_by(unb="u1").first()
        assert found.nick == "zwyyy"
        assert found.is_returning_buyer is True

    def test_unique_unb(self, db_session):
        from app.models.xianyu_tables import XianyuBuyerProfile
        from sqlalchemy.exc import IntegrityError
        p1 = XianyuBuyerProfile(unb="u1", nick="n1")
        db_session.add(p1)
        db_session.commit()
        p2 = XianyuBuyerProfile(unb="u1", nick="n2")
        db_session.add(p2)
        with pytest.raises(IntegrityError):
            db_session.commit()


# ============================================================
# 2. XianyuConversation（消息 + tip）
# ============================================================
class TestConversation:
    def test_create_text_message(self, db_session):
        from app.models.xianyu_tables import XianyuConversation
        m = XianyuConversation(
            unb="u1", cid="c1", role="buyer", message_text="在吗"
        )
        db_session.add(m)
        db_session.commit()
        assert m.id is not None

    def test_create_tip_event(self, db_session):
        from app.models.xianyu_tables import XianyuConversation
        t = XianyuConversation(
            unb="u1", cid="c1", role="system",
            is_tip=True, tip_type="shipped",
            message_text="卖家已发货"
        )
        db_session.add(t)
        db_session.commit()
        assert t.is_tip is True
        assert t.tip_type == "shipped"

    def test_query_by_unb_cid(self, db_session):
        from app.models.xianyu_tables import XianyuConversation
        for i in range(3):
            db_session.add(XianyuConversation(
                unb="u1", cid="c1", role="buyer",
                message_text=f"msg{i}", created_at=datetime.datetime(2024,1,1,i+1),
            ))
        db_session.commit()
        msgs = db_session.query(XianyuConversation).filter_by(
            unb="u1", cid="c1"
        ).order_by(XianyuConversation.created_at).all()
        assert len(msgs) == 3


# ============================================================
# 3. XianyuItem（商品话术）
# ============================================================
class TestItem:
    def test_create_item(self, db_session):
        from app.models.xianyu_tables import XianyuItem
        item = XianyuItem(
            item_id="945237003804",
            title="注塑模具设计修模改模打火花",
            item_prompt="材质：45 号钢，硬度 HRC58-62，价格 ¥10 起",
        )
        db_session.add(item)
        db_session.commit()
        assert item.id is not None
        assert "45 号钢" in item.item_prompt

    def test_unique_item_id(self, db_session):
        from app.models.xianyu_tables import XianyuItem
        from sqlalchemy.exc import IntegrityError
        i1 = XianyuItem(item_id="I1", title="t1")
        db_session.add(i1)
        db_session.commit()
        i2 = XianyuItem(item_id="I1", title="t2")
        db_session.add(i2)
        with pytest.raises(IntegrityError):
            db_session.commit()
