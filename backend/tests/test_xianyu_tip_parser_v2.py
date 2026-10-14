"""
XianyuTipParser V16.3 测试 — 5 态订单事件 + 落库

按 CLAUDE.md 调试铁律：纯逻辑 + SQLite in-memory
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models import xianyu_order  # noqa - 注册
from app.services.xianyu.tip_parser import XianyuTipParser, OrderStatus


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


# =============================================================
# 1. 单 tip 模式识别
# =============================================================
class TestParseTip:
    @pytest.mark.parametrize("text,expected", [
        ("我已付款，请添加自提信息", OrderStatus.PAID),
        ("订单已创建", OrderStatus.PAID),
        ("等待卖家发货", OrderStatus.PAID),
        ("付款成功", OrderStatus.PAID),
        ("卖家已发货", OrderStatus.SHIPPED),
        ("已发货", OrderStatus.SHIPPED),
        ("商品已寄出", OrderStatus.SHIPPED),
        ("订单已签收", OrderStatus.SIGNED),
        ("已签收", OrderStatus.SIGNED),
        ("你已确认收货，交易成功", OrderStatus.RECEIVED),
        ("交易成功", OrderStatus.RECEIVED),
        ("退款成功", OrderStatus.REFUNDED),
        ("钱款已原路退返", OrderStatus.REFUNDED),
        ("原路退回", OrderStatus.REFUNDED),
        ("已退款", OrderStatus.REFUNDED),
    ])
    def test_recognize_all_statuses(self, text, expected):
        """识别 5 态订单事件"""
        result = XianyuTipParser.parse_tip(text)
        assert result == expected

    def test_unknown_text_returns_none(self):
        """未知文本 → None"""
        assert XianyuTipParser.parse_tip("在吗") is None
        assert XianyuTipParser.parse_tip("能便宜吗") is None

    def test_priority_refunded_over_signed(self):
        """'退款成功' + '已签收' 同时出现 → 返回 refunded（优先级最高）"""
        result = XianyuTipParser.parse_tip("订单已签收。退款成功。")
        assert result == OrderStatus.REFUNDED

    def test_priority_signed_over_shipped(self):
        """'已发货' + '已签收' → 返回 signed"""
        result = XianyuTipParser.parse_tip("已发货。已签收。")
        assert result == OrderStatus.SIGNED


# =============================================================
# 2. 提取订单号 + item_id
# =============================================================
class TestExtractFromContent:
    def test_extract_order_no_from_fleamarket_url(self):
        """从 textCard 的 fleamarket:// 链接提 order_no"""
        content = (
            '<strong><font color="#999999">3天后自动确认收货如有问题可延长收货 '
            '<a href="fleamarket://order_detail?id=4303894788218384306&role=Buyer" '
            'target="_blank">查看详情</a></font></strong>'
        )
        order_no = XianyuTipParser.extract_order_no(content)
        assert order_no == "4303894788218384306"

    def test_extract_order_no_returns_none(self):
        """无链接 → None"""
        assert XianyuTipParser.extract_order_no("") is None
        assert XianyuTipParser.extract_order_no("这是普通文本") is None


# =============================================================
# 3. parse_and_persist：消息历史 → 落库
# =============================================================
class TestParseAndPersist:
    def test_single_tip_event_persisted(self, db_session):
        """单条 tip 落库"""
        messages = [{
            "message_id": "m1",
            "send_user_id": "u1",
            "message": {
                "tip": {"tip": "我已付款"},
                "textCard": {
                    "content": '<a href="fleamarket://order_detail?id=O1">详情</a>'
                },
            },
        }]
        orders = XianyuTipParser.parse_and_persist(
            db=db_session, seller_unb="s1", messages=messages
        )
        db_session.commit()
        assert len(orders) == 1
        assert orders[0].status == OrderStatus.PAID
        assert orders[0].order_no == "O1"
        assert orders[0].unb == "u1"
        assert orders[0].seller_unb == "s1"

    def test_multiple_events_upgrade_status(self, db_session):
        """多条 tip：paid → shipped → signed 升级"""
        messages = [
            {"message_id": "m1", "send_user_id": "u1",
             "message": {"tip": {"tip": "我已付款"},
                         "textCard": {"content": '<a href="fleamarket://order_detail?id=O1">详情</a>'}}},
            {"message_id": "m2", "send_user_id": "u1",
             "message": {"tip": {"tip": "卖家已发货"}}},
            {"message_id": "m3", "send_user_id": "u1",
             "message": {"tip": {"tip": "订单已签收"}}},
        ]
        orders = XianyuTipParser.parse_and_persist(
            db=db_session, seller_unb="s1", messages=messages
        )
        db_session.commit()
        assert len(orders) == 1  # 同 order_no 幂等
        # 最终状态：signed（最新）
        assert orders[0].status == OrderStatus.SIGNED

    def test_refund_event_persisted(self, db_session):
        """退款成功 → 落库 refunded"""
        messages = [
            {"message_id": "m1", "send_user_id": "u1",
             "message": {"tip": {"tip": "我已付款"},
                         "textCard": {"content": '<a href="fleamarket://order_detail?id=O1">详情</a>'}}},
            {"message_id": "m2", "send_user_id": "u1",
             "message": {"tip": {"tip": "退款成功，钱款已原路退返"}}},
        ]
        orders = XianyuTipParser.parse_and_persist(
            db=db_session, seller_unb="s1", messages=messages
        )
        db_session.commit()
        assert orders[0].status == OrderStatus.REFUNDED

    def test_idempotent_same_tip(self, db_session):
        """同 tip 重复出现 → 幂等（不重复创建）"""
        messages = [
            {"message_id": "m1", "send_user_id": "u1",
             "message": {"tip": {"tip": "我已付款"},
                         "textCard": {"content": '<a href="fleamarket://order_detail?id=O1">详情</a>'}}},
            {"message_id": "m1-dup", "send_user_id": "u1",
             "message": {"tip": {"tip": "我已付款"},
                         "textCard": {"content": '<a href="fleamarket://order_detail?id=O1">详情</a>'}}},
        ]
        orders = XianyuTipParser.parse_and_persist(
            db=db_session, seller_unb="s1", messages=messages
        )
        db_session.commit()
        # 同一订单只 1 行
        assert len(orders) == 1

    def test_skip_non_order_messages(self, db_session):
        """非订单消息（闲聊/图片/无 unb）→ 跳过"""
        messages = [
            {"message_id": "m1", "send_user_id": "u1",
             "message": {"text": {"text": "在吗"}}},  # 文本无 tip
            {"message_id": "m2", "send_user_id": "",  # 无 unb
             "message": {"tip": {"tip": "我已付款"}}},
            {"message_id": "m3", "send_user_id": "u1",
             "message": {}},  # 空
        ]
        orders = XianyuTipParser.parse_and_persist(
            db=db_session, seller_unb="s1", messages=messages
        )
        db_session.commit()
        assert len(orders) == 0

    def test_no_order_no_skipped(self, db_session):
        """无 order_no 的 tip 跳过（必须有订单号才能落库）"""
        messages = [{
            "message_id": "m1", "send_user_id": "u1",
            "message": {"tip": {"tip": "我已付款"}},  # 无 textCard
        }]
        orders = XianyuTipParser.parse_and_persist(
            db=db_session, seller_unb="s1", messages=messages
        )
        db_session.commit()
        # 无订单号 → 跳过（不能落库）
        assert len(orders) == 0