"""
XianyuOrder ORM + Repository 测试

按 CLAUDE.md 调试铁律：SQLite in-memory 测试
"""
import pytest

from app.models.xianyu_order import XianyuOrder
from app.services.xianyu.order_repository import XianyuOrderRepository


@pytest.fixture
def db_session():
    """SQLite in-memory session（与 conftest.py 一致）"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.base import Base
    from app.models import xianyu_order  # noqa

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


# =============================================================
# 1. ORM 字段
# =============================================================
class TestModelFields:
    def test_required_fields(self):
        """必需字段：unb, seller_unb, status, created_at"""
        o = XianyuOrder(
            unb="u1", seller_unb="s1", status="paid"
        )
        assert o.unb == "u1"
        assert o.seller_unb == "s1"
        assert o.status == "paid"

    def test_optional_fields(self):
        """可选字段：item_id, order_no, amount, tip_source"""
        o = XianyuOrder(
            unb="u1", seller_unb="s1", status="refunded",
            order_no="ORD123", amount=99.00,
            tip_source="退款成功，钱款已原路退返",
        )
        assert o.order_no == "ORD123"
        assert float(o.amount) == 99.00

    def test_default_deleted(self, db_session):
        """deleted 默认 0（未删）"""
        from app.services.xianyu.order_repository import XianyuOrderRepository
        o = XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        db_session.commit()
        # 重新查
        fresh = db_session.query(XianyuOrder).filter_by(id=o.id).first()
        assert fresh.deleted == 0


# =============================================================
# 2. Repository: upsert_from_tip（幂等）
# =============================================================
class TestUpsertFromTip:
    def test_create_new_order(self, db_session):
        """新订单落库"""
        order = XianyuOrderRepository.upsert_from_tip(
            db=db_session,
            seller_unb="s1", unb="u1", item_id="I1",
            order_no="O1", new_status="paid",
            tip_source="我已付款", raw_message_id="m1",
        )
        db_session.commit()
        assert order.id is not None
        assert order.status == "paid"

    def test_idempotent_same_status(self, db_session):
        """同 (seller_unb, unb, order_no) + 同 status → 不重复"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "我已付款", "m1"
        )
        db_session.commit()
        # 第二次同样参数
        order2 = XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "我已付款", "m1"
        )
        db_session.commit()
        # 应该同一条记录
        assert order2.id == 1  # 自增 id=1

    def test_status_upgrade_paid_to_shipped(self, db_session):
        """paid → shipped（状态升级）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "我已付款", "m1"
        )
        db_session.commit()
        order2 = XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "shipped", "卖家已发货", "m2"
        )
        db_session.commit()
        assert order2.status == "shipped"

    def test_status_downgrade_to_refunded(self, db_session):
        """任意状态 → refunded（降级到已退款）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "我已付款", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "shipped", "已发货", "m2"
        )
        db_session.commit()
        order3 = XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "refunded", "退款成功", "m3"
        )
        db_session.commit()
        assert order3.status == "refunded"

    def test_no_downgrade_after_refunded(self, db_session):
        """refunded 后不会回退到 paid/shipped（防攻击）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "paid", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "refunded", "refund", "m2"
        )
        db_session.commit()
        # 攻击：试图回退到 paid
        order3 = XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "fake paid", "m3"
        )
        db_session.commit()
        # 仍然是 refunded
        assert order3.status == "refunded"

    def test_different_order_no_separate_row(self, db_session):
        """不同 order_no 是不同记录"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "paid1", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O2", "paid", "paid2", "m2"
        )
        db_session.commit()
        orders = XianyuOrderRepository.list_by_buyer(db_session, "s1", "u1")
        assert len(orders) == 2


# =============================================================
# 3. Repository: list_by_buyer
# =============================================================
class TestListByBuyer:
    def test_list_returns_only_this_buyer(self, db_session):
        """只返该买家的订单"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p1", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u2", "I1", "O2", "paid", "p2", "m2"
        )
        db_session.commit()
        orders = XianyuOrderRepository.list_by_buyer(db_session, "s1", "u1")
        assert len(orders) == 1
        assert orders[0].unb == "u1"

    def test_list_excludes_deleted(self, db_session):
        """软删除的不返"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p1", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "refunded", "r1", "m2"
        )
        db_session.commit()
        orders = XianyuOrderRepository.list_by_buyer(db_session, "s1", "u1")
        assert len(orders) == 1
        assert orders[0].status == "refunded"

    def test_list_order_by_updated_desc(self, db_session):
        """按 updated_at 倒序（最新在前）"""
        import datetime
        # 直接构造两条订单，手动设 updated_at
        o1 = XianyuOrder(
            seller_unb="s1", unb="u1", order_no="O1", status="paid",
            updated_at=datetime.datetime(2024, 1, 1),
        )
        o2 = XianyuOrder(
            seller_unb="s1", unb="u1", order_no="O2", status="paid",
            updated_at=datetime.datetime(2024, 6, 1),  # 后
        )
        db_session.add(o1)
        db_session.add(o2)
        db_session.commit()
        orders = XianyuOrderRepository.list_by_buyer(db_session, "s1", "u1")
        # O2 是后插入，应该在前
        assert orders[0].order_no == "O2"
        assert orders[1].order_no == "O1"


# =============================================================
# 4. Repository: 业务查询方法
# =============================================================
class TestBusinessQueries:
    def test_get_latest_refunded(self, db_session):
        """查该买家最近一笔已退款订单（关键：用于"重复退款"检测）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "refunded", "r", "m2"
        )
        db_session.commit()
        refunded = XianyuOrderRepository.get_latest_refunded(
            db_session, "s1", "u1"
        )
        assert refunded is not None
        assert refunded.status == "refunded"
        assert refunded.order_no == "O1"

    def test_get_latest_refunded_returns_none_if_no_refund(self, db_session):
        """没有已退款订单 → 返 None（不能误判）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        db_session.commit()
        result = XianyuOrderRepository.get_latest_refunded(
            db_session, "s1", "u1"
        )
        assert result is None

    def test_get_active_order(self, db_session):
        """查最近一笔非退款订单（用于"未退款的订单"决策）"""
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "paid", "p", "m1"
        )
        XianyuOrderRepository.upsert_from_tip(
            db_session, "s1", "u1", "I1", "O1", "shipped", "s", "m2"
        )
        db_session.commit()
        active = XianyuOrderRepository.get_active_order(
            db_session, "s1", "u1"
        )
        assert active is not None
        assert active.status == "shipped"