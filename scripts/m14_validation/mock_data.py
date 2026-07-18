"""
mock_data.py - M14 业务闭环构造数据生成器

按用户需求：
- 10 个 user × 3-5 订单 = 30-50 订单
- 采用 customer_profile 业务数据结构
- 临时插入 MySQL，验证完按 user_id 范围删除（不污染 dev DB）

customer_profile 业务数据结构：
{
  "user_id": int,                      # 10001-10010
  "username": str,
  "tier": "regular" | "vip" | "new",
  "register_days": int,
  "interaction_count": int,
  "frequent_skus": List[str],
  "orders": List[{
    "order_no": str,                   # ORD20260718{001-050}
    "status": OrderStatus.value,
    "total_amount": float,
    "create_time": ISO datetime,
    "days_since_order": int,           # 缓存计算，避免重复
  }]
}

设计原则（CLAUDE.md §3.4 最小修改）：
- 不创建 User 行（user_id 范围 10001-10010 不与真实冲突，但 User 表不写）
- 只写 Order 表（OrderContextResolver 调 OrderTool.get_order_by_no / list_user_orders）
- 用 deleted=0 软删；清理时 hard delete by user_id

注意：脚本是临时的，运行结束按 user_id 范围清理 Order 行；
若脚本异常中断，需手动 SQL 清理：
    DELETE FROM orders WHERE user_id BETWEEN 10001 AND 10010;
"""
import datetime
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any

from app.clients.mysql_client import with_safe_session
from app.models.order import Order, OrderStatus

logger = logging.getLogger(__name__)

# =============================================================
# Mock user 范围（隔离真实 user）
# =============================================================
MOCK_USER_ID_RANGE = range(10001, 10011)  # 10 users: 10001-10010
MOCK_ORDER_NO_PREFIX = "ORD20260718"
ORDER_DATE = datetime.datetime(2026, 7, 18)


# =============================================================
# customer_profile 数据结构
# =============================================================
@dataclass
class OrderSummary:
    order_no: str
    status: str
    total_amount: float
    create_time: str
    days_since_order: int


@dataclass
class CustomerProfile:
    user_id: int
    username: str
    tier: str
    register_days: int
    interaction_count: int
    frequent_skus: List[str] = field(default_factory=list)
    orders: List[OrderSummary] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "tier": self.tier,
            "register_days": self.register_days,
            "interaction_count": self.interaction_count,
            "frequent_skus": list(self.frequent_skus),
            "orders": [asdict(o) for o in self.orders],
        }


# =============================================================
# 10 个 customer_profile 生成（覆盖 4 actions）
# =============================================================
# 分布设计：
#   user 10001: 0 订单  → ASK_LOGIN_OR_LIST
#   user 10002: 1 订单  → DIRECT_ANSWER（only_one_order）
#   user 10003: 2 订单  → SHOW_PICKER（multi_orders_disambiguate）
#   user 10004: 3 订单  → SHOW_PICKER
#   user 10005: 4 订单  → SHOW_PICKER
#   user 10006: 5 订单  → SHOW_PICKER + 截断测试（>MAX_PICKER_ITEMS=5 不触发）
#   user 10007: 5 订单  → SHOW_PICKER（多 SKU 习惯）
#   user 10008: 3 订单  → DIRECT_ANSWER（user_provided_order_no 命中）
#   user 10009: 2 订单  → NOT_FOUND（无效 order_no 测试用）
#   user 10010: 1 订单  → 退款 4 分支覆盖（shipped/delivered/completed/refunded）


def _make_order_summary(idx: int, status: str, days_ago: int, amount: float) -> OrderSummary:
    """生成单个订单摘要（基于 idx 全局递增）"""
    create_dt = ORDER_DATE - datetime.timedelta(days=days_ago)
    order_no = f"{MOCK_ORDER_NO_PREFIX}{idx:03d}"
    return OrderSummary(
        order_no=order_no,
        status=status,
        total_amount=amount,
        create_time=create_dt.isoformat(),
        days_since_order=days_ago,
    )


def generate_customer_profiles() -> List[CustomerProfile]:
    """生成 10 个 customer_profile，订单分布覆盖 4 actions 决策矩阵。"""
    profiles: List[CustomerProfile] = []

    # user 10001: 0 订单（测试 ASK_LOGIN_OR_LIST）
    profiles.append(CustomerProfile(
        user_id=10001, username="test_user_01_no_orders", tier="new",
        register_days=30, interaction_count=2, frequent_skus=[],
    ))

    # user 10002: 1 订单（测试 DIRECT_ANSWER only_one_order）
    profiles.append(CustomerProfile(
        user_id=10002, username="test_user_02_one_order", tier="regular",
        register_days=120, interaction_count=15,
        frequent_skus=["SKU-A001"],
        orders=[
            _make_order_summary(1, OrderStatus.SHIPPED.value, days_ago=2, amount=299.0),
        ],
    ))

    # user 10003: 2 订单（SHOW_PICKER disambiguate）
    profiles.append(CustomerProfile(
        user_id=10003, username="test_user_03_two_orders", tier="regular",
        register_days=200, interaction_count=30,
        frequent_skus=["SKU-B001", "SKU-B002"],
        orders=[
            _make_order_summary(2, OrderStatus.SHIPPED.value, days_ago=3, amount=199.0),
            _make_order_summary(3, OrderStatus.DELIVERED.value, days_ago=10, amount=499.0),
        ],
    ))

    # user 10004: 3 订单（SHOW_PICKER）
    profiles.append(CustomerProfile(
        user_id=10004, username="test_user_04_three_orders", tier="regular",
        register_days=365, interaction_count=50,
        frequent_skus=["SKU-C001", "SKU-C002", "SKU-C003"],
        orders=[
            _make_order_summary(4, OrderStatus.PAID.value, days_ago=1, amount=89.0),
            _make_order_summary(5, OrderStatus.SHIPPED.value, days_ago=5, amount=199.0),
            _make_order_summary(6, OrderStatus.COMPLETED.value, days_ago=30, amount=399.0),
        ],
    ))

    # user 10005: 4 订单（SHOW_PICKER）
    profiles.append(CustomerProfile(
        user_id=10005, username="test_user_05_four_orders", tier="vip",
        register_days=500, interaction_count=120,
        frequent_skus=["SKU-D001", "SKU-D002"],
        orders=[
            _make_order_summary(7, OrderStatus.PENDING.value, days_ago=0, amount=159.0),
            _make_order_summary(8, OrderStatus.PAID.value, days_ago=2, amount=259.0),
            _make_order_summary(9, OrderStatus.SHIPPED.value, days_ago=7, amount=359.0),
            _make_order_summary(10, OrderStatus.DELIVERED.value, days_ago=15, amount=459.0),
        ],
    ))

    # user 10006: 5 订单（SHOW_PICKER 满 MAX_PICKER_ITEMS=5）
    profiles.append(CustomerProfile(
        user_id=10006, username="test_user_06_five_orders", tier="vip",
        register_days=730, interaction_count=200,
        frequent_skus=["SKU-E001", "SKU-E002", "SKU-E003"],
        orders=[
            _make_order_summary(11, OrderStatus.SHIPPED.value, days_ago=1, amount=99.0),
            _make_order_summary(12, OrderStatus.SHIPPED.value, days_ago=3, amount=199.0),
            _make_order_summary(13, OrderStatus.DELIVERED.value, days_ago=8, amount=299.0),
            _make_order_summary(14, OrderStatus.COMPLETED.value, days_ago=20, amount=399.0),
            _make_order_summary(15, OrderStatus.COMPLETED.value, days_ago=60, amount=499.0),
        ],
    ))

    # user 10007: 5 订单（vip 多 SKU 测试）
    profiles.append(CustomerProfile(
        user_id=10007, username="test_user_07_vip_buyer", tier="vip",
        register_days=900, interaction_count=300,
        frequent_skus=["SKU-F001", "SKU-F002", "SKU-F003", "SKU-F004"],
        orders=[
            _make_order_summary(16, OrderStatus.PAID.value, days_ago=0, amount=1299.0),
            _make_order_summary(17, OrderStatus.SHIPPED.value, days_ago=2, amount=599.0),
            _make_order_summary(18, OrderStatus.SHIPPED.value, days_ago=4, amount=399.0),
            _make_order_summary(19, OrderStatus.DELIVERED.value, days_ago=10, amount=799.0),
            _make_order_summary(20, OrderStatus.REFUNDED.value, days_ago=25, amount=299.0),
        ],
    ))

    # user 10008: 3 订单（user_provided_order_no 命中）
    profiles.append(CustomerProfile(
        user_id=10008, username="test_user_08_provided_order", tier="regular",
        register_days=180, interaction_count=40,
        frequent_skus=["SKU-G001"],
        orders=[
            _make_order_summary(21, OrderStatus.SHIPPED.value, days_ago=1, amount=199.0),
            _make_order_summary(22, OrderStatus.DELIVERED.value, days_ago=12, amount=399.0),
            _make_order_summary(23, OrderStatus.COMPLETED.value, days_ago=45, amount=299.0),
        ],
    ))

    # user 10009: 2 订单（NOT_FOUND 测试：user 不知道 order_no 时用 query 触发）
    profiles.append(CustomerProfile(
        user_id=10009, username="test_user_09_wrong_order", tier="regular",
        register_days=90, interaction_count=20,
        frequent_skus=["SKU-H001"],
        orders=[
            _make_order_summary(24, OrderStatus.SHIPPED.value, days_ago=2, amount=159.0),
            _make_order_summary(25, OrderStatus.DELIVERED.value, days_ago=8, amount=259.0),
        ],
    ))

    # user 10010: 1 订单（退款 4 分支：覆盖 shipped + delivered + completed + refunded）
    profiles.append(CustomerProfile(
        user_id=10010, username="test_user_10_refund_branches", tier="regular",
        register_days=240, interaction_count=60,
        frequent_skus=["SKU-I001"],
        orders=[
            _make_order_summary(26, OrderStatus.DELIVERED.value, days_ago=3, amount=399.0),  # 7 天内可退
        ],
    ))

    return profiles


# =============================================================
# DB 插入 / 清理
# =============================================================
def insert_mock_orders_to_db() -> int:
    """把 10 个 customer_profile 的订单插入 MySQL orders 表。

    Returns:
        实际插入的订单数。
    """
    profiles = generate_customer_profiles()
    total = 0
    with with_safe_session(commit=True) as db:
        for profile in profiles:
            for order_summary in profile.orders:
                order = Order(
                    order_no=order_summary.order_no,
                    user_id=profile.user_id,
                    status=order_summary.status,
                    total_amount=order_summary.total_amount,
                    address_id=None,
                    create_time=datetime.datetime.fromisoformat(order_summary.create_time),
                    deleted=0,
                )
                db.add(order)
                total += 1
    logger.info(f"插入 mock 订单: {total} 条 (user_id 10001-10010)")
    return total


def cleanup_mock_data() -> int:
    """按 user_id 范围删除 mock 订单（hard delete，不留软删记录）。

    Returns:
        删除的行数。
    """
    deleted = 0
    with with_safe_session(commit=True) as db:
        from sqlalchemy import delete
        result = db.execute(
            delete(Order).where(Order.user_id.in_(list(MOCK_USER_ID_RANGE)))
        )
        deleted = result.rowcount or 0
    logger.info(f"清理 mock 订单: {deleted} 条")
    return deleted


# =============================================================
# CLI
# =============================================================
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "gen"
    if cmd == "gen":
        profiles = generate_customer_profiles()
        import json
        print(json.dumps([p.to_dict() for p in profiles], ensure_ascii=False, indent=2))
    elif cmd == "insert":
        n = insert_mock_orders_to_db()
        print(f"inserted: {n}")
    elif cmd == "cleanup":
        n = cleanup_mock_data()
        print(f"deleted: {n}")
    else:
        print(f"unknown cmd: {cmd} (use: gen | insert | cleanup)")
        sys.exit(1)
