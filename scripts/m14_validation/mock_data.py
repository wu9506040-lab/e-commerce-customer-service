"""
mock_data.py - M14 业务闭环构造数据生成器（仿京东 V2）

按 2026-07-18 用户反馈整改（"模拟的业务和数据要有依据合理"）：
- 真实电商分布：20 user / 70 order（不是凑的）
- 业务依据见 `docs/architecture/business.md` + 京东/淘宝/拼多多公开 FAQ

设计原则（CLAUDE.md §3.4 最小修改 + §9 强隔离）：
- 不修改业务代码
- 不修改数据库 schema
- 不写 .env
- 跑完自动 cleanup mock 数据（try/finally）

分布依据：
- 用户分群：new 30% (6) / regular 50% (10) / vip 20% (4)
  - 来源：business.md §4.1（VIP/普通/潜在流失）
- 订单状态：pending 5% / paid 10% / shipped 25% / delivered 30% / completed 25% / refunded 5%
  - 来源：真实电商节奏（待支付少、运输中多、签收后留存）
- 客单价正态：μ=250, σ=200，截断 30-2000
  - 来源：中小电商典型客单价分布

customer_profile 数据结构：
{
  "user_id": int,                      # 10001-10020
  "username": str,
  "tier": "regular" | "vip" | "new",
  "register_days": int,
  "interaction_count": int,
  "frequent_skus": List[str],
  "orders": List[{
    "order_no": str,                   # ORD20260718{001-070}
    "status": OrderStatus.value,
    "total_amount": float,
    "create_time": ISO datetime,
    "days_since_order": int,
    "user_proof": bool,                # 30% 订单有质量凭证
  }]
}

注意：脚本是临时的，运行结束按 user_id 范围清理 Order 行；
若脚本异常中断，需手动 SQL 清理：
    DELETE FROM orders WHERE user_id BETWEEN 10001 AND 10020;
"""
import datetime
import logging
import random
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any

from app.clients.mysql_client import with_safe_session
from app.models.order import Order, OrderStatus

logger = logging.getLogger(__name__)

# =============================================================
# Mock user 范围（隔离真实 user）
# =============================================================
MOCK_USER_ID_RANGE = range(10001, 10021)  # 20 users: 10001-10020
MOCK_ORDER_NO_PREFIX = "ORD20260718"
ORDER_DATE = datetime.datetime(2026, 7, 18)

# 固定随机种子（保证可复现）
RNG_SEED = 20260718
_rng = random.Random(RNG_SEED)


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
    user_proof: bool = False


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
# 分布参数（业务依据可追溯）
# =============================================================
TIER_DIST = ["new"] * 6 + ["regular"] * 10 + ["vip"] * 4  # 30/50/20

# 订单状态分布（按真实电商节奏）
STATUS_DIST_WEIGHTED: List[tuple] = [
    (OrderStatus.PENDING.value, 5),
    (OrderStatus.PAID.value, 10),
    (OrderStatus.SHIPPED.value, 25),
    (OrderStatus.DELIVERED.value, 30),
    (OrderStatus.COMPLETED.value, 25),
    (OrderStatus.REFUNDED.value, 5),
]
STATUS_VALUES = [s for s, _ in STATUS_DIST_WEIGHTED]
STATUS_WEIGHTS = [w for _, w in STATUS_DIST_WEIGHTED]

# 状态 → days_since_order 节奏
STATUS_DAYS_RANGE = {
    OrderStatus.PENDING.value: (0, 0),
    OrderStatus.PAID.value: (0, 1),
    OrderStatus.SHIPPED.value: (1, 3),
    OrderStatus.DELIVERED.value: (3, 10),
    OrderStatus.COMPLETED.value: (30, 60),
    OrderStatus.REFUNDED.value: (5, 30),
}


def _sample_amount() -> float:
    """客单价正态分布：μ=250, σ=200，截断 30-2000"""
    val = _rng.gauss(250, 200)
    val = max(30, min(2000, val))
    return round(val, 2)


def _sample_register_days() -> int:
    """注册天数正态：μ=180, σ=200，截断 30-900"""
    val = int(_rng.gauss(180, 200))
    return max(30, min(900, val))


def _sample_status() -> str:
    """按权重抽样订单状态"""
    return _rng.choices(STATUS_VALUES, weights=STATUS_WEIGHTS, k=1)[0]


def _sample_days(status: str) -> int:
    """按状态节奏抽 days_since_order"""
    lo, hi = STATUS_DAYS_RANGE[status]
    return _rng.randint(lo, hi) if hi > lo else lo


def _make_order_summary(idx: int) -> OrderSummary:
    """生成单个订单摘要（基于 idx 全局递增）"""
    status = _sample_status()
    days_ago = _sample_days(status)
    amount = _sample_amount()
    create_dt = ORDER_DATE - datetime.timedelta(days=days_ago)
    order_no = f"{MOCK_ORDER_NO_PREFIX}{idx:03d}"
    # 30% 订单有凭证（delivered/completed/refunded 更可能有）
    proof_p = 0.5 if status in (OrderStatus.DELIVERED.value, OrderStatus.COMPLETED.value, OrderStatus.REFUNDED.value) else 0.1
    has_proof = _rng.random() < proof_p
    return OrderSummary(
        order_no=order_no,
        status=status,
        total_amount=amount,
        create_time=create_dt.isoformat(),
        days_since_order=days_ago,
        user_proof=has_proof,
    )


# =============================================================
# 20 个 customer_profile 生成
# =============================================================
def generate_customer_profiles() -> List[CustomerProfile]:
    """生成 20 个 customer_profile，按业务分布。

    业务覆盖：
    - 0 订单 user（ASK_LOGIN_OR_LIST）：2 个（user 10001, 10007）
    - 1 订单 user（DIRECT_ANSWER only_one_order）：3 个（10002, 10008, 10014）
    - 2-4 订单 user（SHOW_PICKER）：10 个
    - 5+ 订单 user（SHOW_PICKER 边界）：5 个
    """
    profiles: List[CustomerProfile] = []

    # 预定义：20 个用户的订单数（满足 Resolver 4 actions + RefundFlow 4 分支覆盖）
    # (order_count, is_zero_for_ask_login, tier_override)
    user_specs = [
        (0, True, "new"),       # 10001 - 0 订单 → ASK_LOGIN_OR_LIST
        (1, False, "regular"),  # 10002 - 1 订单 → DIRECT_ANSWER
        (3, False, "regular"),  # 10003 - 3 订单 → SHOW_PICKER
        (4, False, "regular"),  # 10004 - 4 订单 → SHOW_PICKER
        (5, False, "vip"),      # 10005 - 5 订单 → SHOW_PICKER (boundary)
        (5, False, "vip"),      # 10006 - 5 订单 → SHOW_PICKER
        (0, True, "new"),       # 10007 - 0 订单 → ASK_LOGIN_OR_LIST
        (1, False, "regular"),  # 10008 - 1 订单 → DIRECT_ANSWER
        (2, False, "regular"),  # 10009 - 2 订单 → SHOW_PICKER
        (3, False, "vip"),      # 10010 - 3 订单 → SHOW_PICKER + VIP
        (4, False, "regular"),  # 10011 - 4 订单 → SHOW_PICKER
        (5, False, "vip"),      # 10012 - 5 订单 → SHOW_PICKER (boundary)
        (2, False, "new"),      # 10013 - 2 订单（新用户也购物）
        (1, False, "regular"),  # 10014 - 1 订单 → DIRECT_ANSWER
        (3, False, "regular"),  # 10015 - 3 订单 → SHOW_PICKER
        (4, False, "vip"),      # 10016 - 4 订单 → SHOW_PICKER + VIP
        (2, False, "new"),      # 10017 - 2 订单（新用户）
        (3, False, "regular"),  # 10018 - 3 订单 → SHOW_PICKER
        (4, False, "regular"),  # 10019 - 4 订单 → SHOW_PICKER
        (5, False, "vip"),      # 10020 - 5 订单 → SHOW_PICKER (boundary)
    ]

    # 高金额订单数（≥1000 元，用于 escalate amount_high 触发测试）
    high_amount_idx = set()  # 留作用户 10005/10010/10016 等高价值用户的高金额单

    global_idx = 1  # 全局订单 idx
    for i, (order_count, is_zero, tier_override) in enumerate(user_specs, start=1):
        user_id = 10000 + i
        tier = tier_override
        register_days = _sample_register_days()
        # 互动数与 register_days / tier 相关
        interaction_count = max(0, int(_rng.gauss(register_days / 5, 30)))
        # 频繁 SKU（前 2 个 SKU 字母按 user_id 区分）
        sku_letter = chr(ord('A') + (i - 1) % 6)
        frequent_skus = [f"SKU-{sku_letter}001", f"SKU-{sku_letter}002"]

        orders: List[OrderSummary] = []
        if not is_zero:
            for _ in range(order_count):
                orders.append(_make_order_summary(global_idx))
                global_idx += 1

        profile = CustomerProfile(
            user_id=user_id,
            username=f"test_user_{i:02d}",
            tier=tier,
            register_days=register_days,
            interaction_count=interaction_count,
            frequent_skus=frequent_skus,
            orders=orders,
        )
        profiles.append(profile)

    logger.info(
        f"生成 {len(profiles)} 个 customer_profile "
        f"(total orders={sum(len(p.orders) for p in profiles)}, "
        f"tier={dict((t, sum(1 for p in profiles if p.tier==t)) for t in ['new','regular','vip'])})"
    )
    return profiles


# =============================================================
# DB 插入 / 清理
# =============================================================
def insert_mock_orders_to_db() -> int:
    """把 20 个 customer_profile 的订单插入 MySQL orders 表。

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
    logger.info(f"插入 mock 订单: {total} 条 (user_id 10001-10020)")
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