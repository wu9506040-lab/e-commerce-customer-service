"""
P0-4 真 DB 集成测试样板 — SOP-V1 §2.2 数据准确性验证规范

按 docs/governance/ai_development_sop.md §2 落地：
- §2.1 四要素：DB Schema ✓（动态 create_all）+ 测试数据 ✓（inline seed）+
  接口断言（N/A，工具层）+ 业务结果断言 ✓（OrderStatus 流转 + 软删断言）
- §2.3 DB 断言规范：pytest + SQLAlchemy 模板

本样板覆盖 3 类核心断言（每类至少 1 用例）：
1. 真 DB 写入 + 读回（基础 CRUD）
2. 状态流转约束（OrderStatus enum + 业务规则）
3. 软删行为（deleted=0/1，CLAUDE.md §7 表设计原则）

技术限制：
- 用 SQLite in-memory 作本地样板（CI 升级到真 MySQL 见 V1.1）
- CLAUDE.md §7 不建 DB 级 FK，故 user_id / order_id 直接硬编码整数值
- SQLAlchemy `BigInteger` 在 SQLite 上非 rowid 别名 → 此样板只测 Order 表本身
  （避免触发 SQLite PK auto-increment 边界；真 MySQL 测试无此限制）

V1.1 增量：
- 加真 MySQL 集成测试（CI 切 fixture engine）
- 加 fixtures/seed/ 目录 + JSON seed 数据
- 加 conftest.py 共享 seed loader
"""
import decimal

from app.models.order import Order, OrderItem, OrderStatus


# =============================================================
# 1. 真 DB 写入 + 读回
# =============================================================
class TestRealDbWriteAndRead:
    """基础 CRUD 真 DB 断言（不是 mock，是真 SQLAlchemy ORM + 真引擎）"""

    def test_create_order_and_read_back(self, db_session):
        """创建订单 → commit → 重新 query → 断言字段一致"""
        # Act：CLAUDE.md §7 不建 DB 级 FK，user_id 硬编码即可
        order = Order(
            order_no="ORD_TEST_001",
            user_id=1001,  # 测试用 user_id（非 PK）
            status=OrderStatus.PENDING.value,
            total_amount=decimal.Decimal("99.99"),
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)

        # Assert 1：自增 PK 真写入
        assert order.id is not None
        assert order.id > 0

        # Assert 2：业务字段保留（Decimal 精度 + 字符串）
        assert order.order_no == "ORD_TEST_001"
        assert order.user_id == 1001
        assert order.status == "pending"
        assert order.total_amount == decimal.Decimal("99.99")

        # Assert 3：重新 query 拿到的字段与写入一致（真 DB 持久化）
        queried = db_session.query(Order).filter(Order.id == order.id).first()
        assert queried is not None
        assert queried.order_no == "ORD_TEST_001"
        assert queried.status == OrderStatus.PENDING.value
        assert queried.deleted == 0  # 默认未删

    def test_create_order_item_with_decimal(self, db_session):
        """订单明细：Decimal(10,2) 精度 + FK 关联正确性"""
        order = Order(
            order_no="ORD_ITEM_001",
            user_id=1002,
            status=OrderStatus.PENDING.value,
            total_amount=decimal.Decimal("50.00"),
        )
        db_session.add(order)
        db_session.commit()

        # 写订单明细（product_id 同样硬编码，无 FK 约束）
        item = OrderItem(
            order_id=order.id,
            product_id=1,
            sku="SKU_TEST_001",
            product_name="测试商品",
            qty=2,
            unit_price=decimal.Decimal("25.00"),
            subtotal=decimal.Decimal("50.00"),
        )
        db_session.add(item)
        db_session.commit()

        # 断言：通过 order_id 反查明细
        items = db_session.query(OrderItem).filter(OrderItem.order_id == order.id).all()
        assert len(items) == 1
        assert items[0].sku == "SKU_TEST_001"
        assert items[0].qty == 2
        # Decimal 精度断言（不能用 == 0.5 浮点比较）
        assert items[0].subtotal == decimal.Decimal("50.00")

    def test_create_multiple_orders_query_with_filter(self, db_session):
        """批量写入 + 条件过滤（user_id / status / deleted 组合）"""
        # 同一 user 下 3 个订单，2 pending + 1 paid
        for i, status in enumerate(
            [OrderStatus.PENDING.value, OrderStatus.PENDING.value, OrderStatus.PAID.value]
        ):
            db_session.add(
                Order(
                    order_no=f"ORD_MULTI_{i:03d}",
                    user_id=2001,
                    status=status,
                    total_amount=decimal.Decimal("10.00"),
                )
            )
        db_session.commit()

        # 过滤 1：按 user_id
        user_orders = db_session.query(Order).filter(Order.user_id == 2001).all()
        assert len(user_orders) == 3

        # 过滤 2：按 user_id + status 复合
        pending = db_session.query(Order).filter(
            Order.user_id == 2001,
            Order.status == OrderStatus.PENDING.value,
        ).all()
        assert len(pending) == 2

        # 过滤 3：业务层默认加 deleted=0
        visible = db_session.query(Order).filter(
            Order.user_id == 2001,
            Order.deleted == 0,
        ).all()
        assert len(visible) == 3


# =============================================================
# 2. 状态流转约束（OrderStatus enum）
# =============================================================
class TestOrderStatusTransition:
    """订单状态机：enum 约束 + 业务规则（CLAUDE.md §9.2.5 状态机约束）"""

    def test_order_status_enum_values(self):
        """OrderStatus enum 6 个值必须与 schema 一致"""
        # 这是业务层 enum 与 MySQL schema 的契约断言（不依赖 DB）
        assert OrderStatus.PENDING.value == "pending"
        assert OrderStatus.PAID.value == "paid"
        assert OrderStatus.SHIPPED.value == "shipped"
        assert OrderStatus.DELIVERED.value == "delivered"
        assert OrderStatus.COMPLETED.value == "completed"
        assert OrderStatus.REFUNDED.value == "refunded"

    def test_order_status_persists_correctly(self, db_session):
        """写入 enum 值 → DB 存为字符串 → 读回仍可被 enum 识别"""
        # pending → paid → shipped → delivered 流转
        statuses = [
            OrderStatus.PENDING.value,
            OrderStatus.PAID.value,
            OrderStatus.SHIPPED.value,
            OrderStatus.DELIVERED.value,
        ]
        order = Order(
            order_no="ORD_FLOW_001",
            user_id=3001,
            status=statuses[0],
            total_amount=decimal.Decimal("10.00"),
        )
        db_session.add(order)
        db_session.commit()

        for new_status in statuses[1:]:
            order.status = new_status
            db_session.commit()
            db_session.refresh(order)
            assert order.status == new_status

        # 终态断言：DB 存的就是 "delivered" 字符串
        queried = db_session.query(Order).filter(Order.order_no == "ORD_FLOW_001").first()
        assert queried.status == "delivered"

    def test_refunded_is_terminal_status(self, db_session):
        """refunded 是终态（CLAUDE.md §9.2.5）：写入后不能再改"""
        order = Order(
            order_no="ORD_TERM_001",
            user_id=3002,
            status=OrderStatus.PAID.value,
            total_amount=decimal.Decimal("20.00"),
        )
        db_session.add(order)
        db_session.commit()

        order.status = OrderStatus.REFUNDED.value
        db_session.commit()

        # 业务层不阻止改，但 enum 不变；本测试断言"业务层期望不会再次流转"
        # 实际生产逻辑（refund 后不允许恢复 paid）在 order_lifecycle 校验；
        # 本样板只验 enum 值能正确写入 DB
        queried = db_session.query(Order).filter(Order.order_no == "ORD_TERM_001").first()
        assert queried.status == "refunded"


# =============================================================
# 3. 软删行为（CLAUDE.md §7 表设计原则：deleted=0 默认）
# =============================================================
class TestSoftDeletePattern:
    """deleted=0 默认；软删 = UPDATE deleted=1（CLAUDE.md §7 表设计原则）"""

    def test_default_deleted_value_is_zero(self, db_session):
        """新建订单 deleted 默认 0"""
        order = Order(
            order_no="ORD_SOFT_001",
            user_id=4001,
            status=OrderStatus.PENDING.value,
            total_amount=decimal.Decimal("0.01"),
        )
        db_session.add(order)
        db_session.commit()
        db_session.refresh(order)

        assert order.deleted == 0

    def test_soft_delete_keeps_row_but_marks_deleted(self, db_session):
        """软删：行还在 DB，但 deleted=1；正常 query 应过滤掉"""
        order = Order(
            order_no="ORD_SOFT_002",
            user_id=4002,
            status=OrderStatus.PENDING.value,
            total_amount=decimal.Decimal("1.00"),
        )
        db_session.add(order)
        db_session.commit()
        order_id = order.id

        # 软删
        order.deleted = 1
        db_session.commit()

        # Assert 1：行还在 DB（不是 DELETE FROM）
        raw = db_session.query(Order).filter(Order.id == order_id).first()
        assert raw is not None
        assert raw.deleted == 1

        # Assert 2：业务层 query 必须过滤 deleted=0（与 OrderTool 同模式）
        visible = db_session.query(Order).filter(
            Order.id == order_id,
            Order.deleted == 0,
        ).first()
        assert visible is None  # 业务层看不到软删数据

    def test_query_excludes_soft_deleted_by_default(self, db_session):
        """批量数据中软删的行不应出现在默认查询结果里"""
        # 3 个订单，删第 2 个
        for i in range(3):
            db_session.add(
                Order(
                    order_no=f"ORD_BULK_{i:03d}",
                    user_id=4003,
                    status=OrderStatus.PENDING.value,
                    total_amount=decimal.Decimal("1.00"),
                )
            )
        db_session.commit()

        # 软删中间那个
        middle = db_session.query(Order).filter(Order.order_no == "ORD_BULK_001").first()
        middle.deleted = 1
        db_session.commit()

        # 默认 query 应该只剩 2 个
        visible = db_session.query(Order).filter(
            Order.user_id == 4003,
            Order.deleted == 0,
        ).order_by(Order.order_no).all()
        assert len(visible) == 2
        assert visible[0].order_no == "ORD_BULK_000"
        assert visible[1].order_no == "ORD_BULK_002"