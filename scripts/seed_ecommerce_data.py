"""
seed_ecommerce_data.py - 注入电商 mock 数据到 MySQL

数据规模（M1 设计）：
  - 10 商品（对齐 docs/ecommerce_kb/products.json 的 SKU001-SKU010）
  - 5 订单（覆盖 5 个状态：pending/paid/shipped/delivered/refunded）
  - 6 订单明细
  - 1 退款（对应 refunded 订单）

幂等性：
  - 演示数据，先 DELETE 后 INSERT，重跑安全
  - user_id=1 即 admin（02_seed.sql 预置）

用法：
    # 后端 .env 或 deploy/.env.dev 至少有一个
    PYTHONPATH=backend python scripts/seed_ecommerce_data.py
"""
import datetime
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 加载 .env（QWEN_API_KEY / QDRANT_URL / MYSQL 等）
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
    logging.warning("python-dotenv 未安装，跳过 .env 加载")

from sqlalchemy import delete

from app.clients.mysql_client import get_session_local
from app.models import Base  # noqa: F401  触发所有 model 注册
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.refund import Refund, RefundStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# =============================================================
# Mock 数据（与 docs/ecommerce_kb/products.json 对齐）
# =============================================================
PRODUCTS = [
    {"sku": "SKU001", "name": "智选科技 ZP1 旗舰手机 12+256",     "price": 5999, "stock": 100,
     "attrs": {"color": ["星空黑", "雪域白", "极光紫"], "spec": "12+256"}},
    {"sku": "SKU002", "name": "智选科技 ZP2 Pro 拍照手机 8+256", "price": 4299, "stock": 80,
     "attrs": {"color": ["暗夜绿", "月光银"], "spec": "8+256"}},
    {"sku": "SKU003", "name": "智选科技 ZN1 千元机 6+128",       "price": 1299, "stock": 200,
     "attrs": {"color": ["深空灰"], "spec": "6+128"}},
    {"sku": "SKU004", "name": "智选科技 ZN2 老人机 4+64",         "price": 899,  "stock": 150,
     "attrs": {"color": ["典雅黑", "福寿红"], "spec": "4+64"}},
    {"sku": "SKU005", "name": "智选科技 BP1 无线降噪耳机",        "price": 899,  "stock": 300,
     "attrs": {"color": ["星空黑", "珍珠白"]}},
    {"sku": "SKU006", "name": "智选科技 WS1 智能手表",            "price": 1299, "stock": 120,
     "attrs": {"color": ["暗夜黑", "钛灰"]}},
    {"sku": "SKU007", "name": "智选科技 PT1 平板 8+256",          "price": 2499, "stock": 90,
     "attrs": {"color": ["深空灰"], "spec": "8+256"}},
    {"sku": "SKU008", "name": "智选科技 LB1 笔记本 16+512",       "price": 5499, "stock": 60,
     "attrs": {"color": ["钛灰"], "spec": "16+512"}},
    {"sku": "SKU009", "name": "智选科技 KB1 机械键盘 87 键",      "price": 499,  "stock": 200,
     "attrs": {"axis": "红轴", "layout": "87 TKL"}},
    {"sku": "SKU010", "name": "智选科技 MS1 无线鼠标",            "price": 199,  "stock": 250,
     "attrs": {"weight": "56g", "connection": "三模"}},
]
PRODUCTS_BY_SKU = {p["sku"]: p for p in PRODUCTS}

# 5 订单（user_id=1，挂 admin 账号）
ORDERS = [
    {"order_no": "ORD20260620001", "status": OrderStatus.PENDING.value,   "days_ago": 1,
     "items": [("SKU005", 1)]},
    {"order_no": "ORD20260621002", "status": OrderStatus.PAID.value,     "days_ago": 2,
     "items": [("SKU001", 1), ("SKU005", 1)]},
    {"order_no": "ORD20260622003", "status": OrderStatus.SHIPPED.value,  "days_ago": 4,
     "items": [("SKU003", 1)]},
    {"order_no": "ORD20260615004", "status": OrderStatus.DELIVERED.value, "days_ago": 10,
     "items": [("SKU002", 1)]},
    {"order_no": "ORD20260601005", "status": OrderStatus.REFUNDED.value, "days_ago": 25,
     "items": [("SKU009", 1), ("SKU010", 1)]},
]

REFUNDS = [
    {"refund_no": "RF20260605001", "order_no": "ORD20260601005",
     "reason": "键盘轴体异响，使用 3 天", "status": RefundStatus.COMPLETED.value, "amount": 698},
]

USER_ID = 1  # admin（02_seed.sql 预置）


def main() -> int:
    SessionLocal = get_session_local()

    # 0) 幂等建表（已存在则跳过；Demo 阶段绕开手工 SQL 迁移）
    from app.clients.mysql_client import get_engine
    logger.info("建表检查（Base.metadata.create_all，幂等）...")
    Base.metadata.create_all(bind=get_engine())
    logger.info("建表完成")

    # 1) 清空旧 mock 数据（先删子表，后删父表；演示用，可重建）
    logger.info("清空旧 mock 数据...")
    with SessionLocal() as db:
        try:
            db.execute(delete(Refund))
            db.execute(delete(OrderItem))
            db.execute(delete(Order))
            db.execute(delete(Product))
            db.commit()
            logger.info("清空完成")
        except Exception as e:
            db.rollback()
            logger.exception(f"清空失败: {e}")
            return 1

    # 2) 插入商品
    logger.info(f"插入 {len(PRODUCTS)} 个商品...")
    sku_to_id: dict[str, int] = {}
    with SessionLocal() as db:
        try:
            for p in PRODUCTS:
                obj = Product(
                    sku=p["sku"],
                    name=p["name"],
                    description=f"{p['name']}，库存 {p['stock']}",
                    price=p["price"],
                    attributes=p["attrs"],
                    review_text=None,  # V2.x 不入 RAG，独立字段暂留空
                    stock=p["stock"],
                    status=1,
                )
                db.add(obj)
                db.flush()
                sku_to_id[p["sku"]] = obj.id
            db.commit()
            logger.info(f"商品插入完成: {sku_to_id}")
        except Exception as e:
            db.rollback()
            logger.exception(f"商品插入失败: {e}")
            return 1

    # 3) 插入订单 + 明细
    logger.info(f"插入 {len(ORDERS)} 个订单...")
    order_id_map: dict[str, int] = {}
    with SessionLocal() as db:
        try:
            for o in ORDERS:
                total = sum(qty * PRODUCTS_BY_SKU[sku]["price"] for sku, qty in o["items"])
                order = Order(
                    order_no=o["order_no"],
                    user_id=USER_ID,
                    status=o["status"],
                    total_amount=total,
                    create_time=datetime.datetime.now() - datetime.timedelta(days=o["days_ago"]),
                )
                db.add(order)
                db.flush()
                order_id_map[o["order_no"]] = order.id

                for sku, qty in o["items"]:
                    product = PRODUCTS_BY_SKU[sku]
                    unit_price = product["price"]
                    item = OrderItem(
                        order_id=order.id,
                        product_id=sku_to_id[sku],
                        sku=sku,
                        product_name=product["name"],
                        qty=qty,
                        unit_price=unit_price,
                        subtotal=qty * unit_price,
                    )
                    db.add(item)
            db.commit()
            logger.info(f"订单插入完成: {order_id_map}")
        except Exception as e:
            db.rollback()
            logger.exception(f"订单插入失败: {e}")
            return 1

    # 4) 插入退款
    logger.info(f"插入 {len(REFUNDS)} 个退款...")
    with SessionLocal() as db:
        try:
            for r in REFUNDS:
                refund = Refund(
                    refund_no=r["refund_no"],
                    order_id=order_id_map[r["order_no"]],
                    user_id=USER_ID,
                    reason=r["reason"],
                    status=r["status"],
                    amount=r["amount"],
                )
                db.add(refund)
            db.commit()
            logger.info("退款插入完成")
        except Exception as e:
            db.rollback()
            logger.exception(f"退款插入失败: {e}")
            return 1

    # 5) 验证
    logger.info("=" * 60)
    logger.info("验证")
    logger.info("=" * 60)
    with SessionLocal() as db:
        p_count = db.query(Product).filter(Product.deleted == 0).count()
        o_count = db.query(Order).filter(Order.deleted == 0).count()
        i_count = db.query(OrderItem).filter(OrderItem.deleted == 0).count()
        r_count = db.query(Refund).filter(Refund.deleted == 0).count()
        logger.info(f"products  = {p_count}  (期望 10)")
        logger.info(f"orders    = {o_count}  (期望 5)")
        logger.info(f"order_items = {i_count}  (期望 6)")
        logger.info(f"refunds   = {r_count}  (期望 1)")

        # 状态分布
        from sqlalchemy import func as sqlfunc
        status_rows = db.query(Order.status, sqlfunc.count(Order.id)).filter(
            Order.deleted == 0
        ).group_by(Order.status).all()
        logger.info("订单状态分布:")
        for status, count in status_rows:
            logger.info(f"  {status:12s} = {count}")

    return 0


if __name__ == "__main__":
    sys.exit(main())