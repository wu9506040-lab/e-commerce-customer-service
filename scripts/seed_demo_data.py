#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
seed_demo_data.py - 给演示用户注入丰富的订单 + 历史会话数据（M9.5）

为什么这个脚本：
  - 之前 convtest / demotest 用户没有任何订单 → 个人中心空 → 用户体感"只是个 demo"
  - 之前没有历史会话 → 左侧会话列表空 → 用户不知道产品长什么样
  - 解决方案：给演示用户一次性塞 7 个不同状态的订单 + 3 条历史会话
  - 一键 idempotent（先查后插，重跑安全）

数据范围：
  - 7 订单（覆盖所有状态 + 演示退款对话）：
    ORD...01 待支付 / 02 已支付 / 03 运输中 / 04 已签收 / 05 已退款 / 06 已完成 / 07 待支付
  - 3 历史会话：
    ① "ZP1 续航怎么样"  → 商品咨询
    ② "ORD20260621002 现在到哪了" → 订单查询
    ③ "怎么申请退款" → 政策问答

用法（Windows + Docker MySQL）：
    PYTHONIOENCODING=utf-8 PYTHONPATH=backend python scripts/seed_demo_data.py
    # 默认用户 demotest；指定其他用户：--username convtest
    PYTHONIOENCODING=utf-8 PYTHONPATH=backend python scripts/seed_demo_data.py --username convtest
"""
import argparse
import datetime as dt
import logging
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 加载 .env（DATABASE_URL 等）
try:
    from dotenv import load_dotenv  # type: ignore
    for env_file in [
        BACKEND_DIR / ".env",
        PROJECT_ROOT / "deploy" / ".env.dev",
        PROJECT_ROOT / ".env",
    ]:
        if env_file.exists():
            load_dotenv(env_file)
            logging.info(f"已加载环境变量: {env_file}")
            break
    else:
        logging.warning("未找到 .env 文件，依赖系统环境变量")
except ImportError:
    pass

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.clients.mysql_client import get_engine, get_session_local  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.message import Message  # noqa: E402
from app.models.order import Order, OrderItem, OrderStatus  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.user import User  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("seed_demo")


# =============================================================
# 演示订单模板（覆盖所有状态）
# =============================================================
def _build_orders() -> list[dict]:
    """构造 7 个演示订单的模板数据"""
    today = dt.date.today()
    return [
        # 1. 待支付（最近）
        {
            "order_no": f"ORD{today.strftime('%Y%m%d')}001",
            "status": OrderStatus.PENDING.value,
            "days_ago": 0,
            "items": [("SKU005", 1), ("SKU009", 1)],  # BP1 耳机 + KB1 键盘
        },
        # 2. 已支付
        {
            "order_no": f"ORD{today.strftime('%Y%m%d')}002",
            "status": OrderStatus.PAID.value,
            "days_ago": 1,
            "items": [("SKU002", 1)],  # ZP2 Pro
        },
        # 3. 运输中
        {
            "order_no": f"ORD{today.strftime('%Y%m%d')}003",
            "status": OrderStatus.SHIPPED.value,
            "days_ago": 2,
            "items": [("SKU006", 1), ("SKU010", 1)],  # WS1 手表 + MS1 鼠标
        },
        # 4. 已签收
        {
            "order_no": f"ORD{today.strftime('%Y%m%d')}004",
            "status": OrderStatus.DELIVERED.value,
            "days_ago": 5,
            "items": [("SKU007", 1)],  # PT1 平板
        },
        # 5. 已完成
        {
            "order_no": f"ORD{today.strftime('%Y%m%d')}005",
            "status": OrderStatus.COMPLETED.value,
            "days_ago": 15,
            "items": [("SKU001", 1)],  # ZP1
        },
        # 6. 已退款（演示退款对话）
        {
            "order_no": f"ORD{today.strftime('%Y%m%d')}006",
            "status": OrderStatus.REFUNDED.value,
            "days_ago": 7,
            "items": [("SKU008", 1)],  # LB1 笔记本
        },
        # 7. 待支付（更早）
        {
            "order_no": f"ORD{today.strftime('%Y%m%d')}007",
            "status": OrderStatus.PENDING.value,
            "days_ago": 3,
            "items": [("SKU003", 2)],  # ZN1 x2
        },
    ]


# =============================================================
# 演示历史会话模板（让侧边栏有内容）
# =============================================================
def _build_conversations() -> list[dict]:
    """3 条历史会话，每条 1~3 轮对话"""
    today = dt.date.today()
    base = dt.datetime.combine(today, dt.time(9, 0))
    return [
        {
            "title": "ZP1 续航怎么样",
            "first_query": "ZP1 续航怎么样",
            "days_ago": 0,
            "messages": [
                ("user", "ZP1 续航怎么样"),
                (
                    "assistant",
                    "ZP1 旗舰手机搭载 5000mAh 大电池，支持 67W 快充。重度使用可续航约 8 小时，轻度使用可达 1.5 天。",
                ),
                ("user", "支持无线充电吗"),
                (
                    "assistant",
                    "ZP1 支持 50W 无线快充和 10W 无线反充（给耳机/手表应急补电）。",
                ),
            ],
        },
        {
            "title": f"ORD{today.strftime('%Y%m%d')}002 物流查询",
            "first_query": f"ORD{today.strftime('%Y%m%d')}002 现在到哪了",
            "days_ago": 1,
            "messages": [
                ("user", f"ORD{today.strftime('%Y%m%d')}002 现在到哪了"),
                (
                    "assistant",
                    f"订单 ORD{today.strftime('%Y%m%d')}002 当前状态：已支付，仓库待发货。预计 24 小时内出库。",
                ),
            ],
        },
        {
            "title": "退款流程咨询",
            "first_query": "怎么申请退款",
            "days_ago": 7,
            "messages": [
                ("user", "怎么申请退款"),
                (
                    "assistant",
                    "您好！7 天无理由退款的流程：\n1. 进入【我的订单】找到对应订单\n2. 点击【申请退款】并选择原因\n3. 寄回商品（运费由责任方承担）\n4. 仓库验收后 1-3 个工作日原路退款",
                ),
                ("user", "运费谁出"),
                (
                    "assistant",
                    "7 天无理由退货：商品质量问题运费由商家承担；个人原因退货运费由买家承担。",
                ),
            ],
        },
    ]


# =============================================================
# 幂等性工具
# =============================================================
def _upsert_order(db: Session, user_id: int, template: dict) -> Order:
    """订单已存在则跳过，否则创建（基于 order_no 唯一键）"""
    existing = db.scalar(
        select(Order).where(
            Order.order_no == template["order_no"],
            Order.deleted == 0,
        )
    )
    if existing is not None:
        logger.info(f"  订单已存在，跳过: {template['order_no']}")
        return existing

    create_time = dt.datetime.now() - dt.timedelta(days=template["days_ago"])
    order = Order(
        order_no=template["order_no"],
        user_id=user_id,
        status=template["status"],
        total_amount=0,  # 先占位，下面 items 算出来后 update
        create_time=create_time,
        update_time=create_time,
        deleted=0,
    )
    db.add(order)
    db.flush()  # 拿 order.id

    # items
    total = 0.0
    for sku, qty in template["items"]:
        prod = db.scalar(select(Product).where(Product.sku == sku, Product.deleted == 0))
        if prod is None:
            logger.warning(f"  商品不存在: {sku}，跳过该 item")
            continue
        unit_price = float(prod.price)
        subtotal = unit_price * qty
        total += subtotal
        db.add(OrderItem(
            order_id=order.id,
            product_id=prod.id,
            sku=sku,
            product_name=prod.name,
            qty=qty,
            unit_price=unit_price,
            subtotal=subtotal,
            create_time=create_time,
            update_time=create_time,
            deleted=0,
        ))

    order.total_amount = total
    db.flush()
    logger.info(
        f"  创建订单: {order.order_no} status={order.status} "
        f"items={len(template['items'])} total=¥{total:.2f}"
    )
    return order


def _upsert_conversation(db: Session, user_id: int, template: dict) -> None:
    """会话已存在则跳过（基于 title + user_id），否则创建并插入消息"""
    # 用 uuid4 hex 当 session_id
    existing = db.scalar(
        select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.title == template["title"],
            Conversation.deleted == 0,
        )
    )
    if existing is not None:
        logger.info(f"  会话已存在，跳过: {template['title']}")
        return

    session_id = uuid.uuid4().hex
    base_time = dt.datetime.now() - dt.timedelta(days=template["days_ago"])
    msgs = template["messages"]
    last_msg_at = base_time + dt.timedelta(seconds=30 * len(msgs))

    conv = Conversation(
        session_id=session_id,
        user_id=user_id,
        title=template["title"],
        status=1,
        message_count=len(msgs),
        first_query=template["first_query"],
        last_message_at=last_msg_at,
        create_time=base_time,
        update_time=last_msg_at,
        deleted=0,
    )
    db.add(conv)

    # 逐条消息（user/assistant 间隔 30s）
    for idx, (role, content) in enumerate(msgs):
        msg_time = base_time + dt.timedelta(seconds=30 * idx)
        db.add(Message(
            session_id=session_id,
            user_id=user_id,
            role=role,
            content=content,
            create_time=msg_time,
            update_time=msg_time,
            deleted=0,
        ))

    logger.info(f"  创建会话: {template['title']} ({len(msgs)} 条消息)")


# =============================================================
# 主流程
# =============================================================
def seed(username: str) -> int:
    """注入演示数据；返回 user_id"""
    engine = get_engine()
    SessionLocal = get_session_local()

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            raise SystemExit(
                f"用户 {username} 不存在，请先注册或改用 --username 指定其他用户"
            )
        user_id = user.id
        logger.info(f"目标用户: {username} (id={user_id})")

        # ---------- 1. 订单 ----------
        logger.info("=" * 60)
        logger.info("Step 1/2: 注入演示订单")
        logger.info("=" * 60)
        for tpl in _build_orders():
            _upsert_order(db, user_id, tpl)
        db.commit()

        # ---------- 2. 会话 ----------
        logger.info("=" * 60)
        logger.info("Step 2/2: 注入演示会话")
        logger.info("=" * 60)
        for tpl in _build_conversations():
            _upsert_conversation(db, user_id, tpl)
        db.commit()

        # ---------- 3. 汇总 ----------
        order_count = db.scalar(
            select(Order).where(Order.user_id == user_id, Order.deleted == 0)
        )
        # 实际数量（重新查一次）
        from sqlalchemy import func
        total_orders = db.scalar(
            select(func.count(Order.id)).where(
                Order.user_id == user_id, Order.deleted == 0
            )
        )
        total_convs = db.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.user_id == user_id, Conversation.deleted == 0
            )
        )
        logger.info("=" * 60)
        logger.info(f"完成：用户 {username} 现在有 {total_orders} 个订单 / {total_convs} 个会话")
        logger.info("=" * 60)
        return user_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="注入演示订单 + 会话")
    parser.add_argument(
        "--username",
        default="demotest",
        help="目标用户名（默认 demotest）",
    )
    args = parser.parse_args()

    seed(args.username)