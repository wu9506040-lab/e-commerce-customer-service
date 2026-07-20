"""
MySQL 默认实现 — MySQLOrderService + MySQLProductService（Sprint 15）

实现 services/order/protocols.py 的 OrderService / ProductService Protocol。
沿用现有 OrderTool/ProductTool 的查询语义（越权防护 / 软删过滤 / 在售过滤），
把 ORM 行映射为 DTO（app/schemas/business.py）。

async 方法内部走 sync `with_safe_session`（clients 层复用），
与 mysql_client 的 sync SDK 风格一致（CLAUDE.md §7.2 clients 只做连接）。
本文件是**唯一**允许 import mysql_client 的 Protocol 实现处（工具类改走 Protocol）。
"""
import logging
from datetime import datetime
from typing import List, Optional

from app.clients.mysql_client import with_safe_session
from app.models.order import Order as OrderORM, OrderItem as OrderItemORM
from app.models.product import Product as ProductORM
from app.schemas.business import Order, OrderItem, Product

logger = logging.getLogger(__name__)


# =============================================================
# ORM → DTO 映射（纯函数）
# =============================================================
def _to_order_item(i: OrderItemORM) -> OrderItem:
    return OrderItem(
        sku=i.sku,
        product_name=i.product_name,
        quantity=i.qty,               # ORM qty → DTO quantity
        unit_price=float(i.unit_price),
        subtotal=float(i.subtotal),
    )


def _to_order(o: OrderORM, items: List[OrderItemORM]) -> Order:
    return Order(
        order_no=o.order_no,
        user_id=o.user_id,
        status=o.status,
        items=[_to_order_item(i) for i in items],
        total_amount=float(o.total_amount),
        shipping_address=None,        # 现有 ORM 只存 address_id，V3+ 补明文地址
        tracking_no=None,             # 现有 ORM 无 tracking_no，物流走 OrderTool.get_logistics mock
        create_time=o.create_time,
        update_time=o.update_time,
    )


def _to_product(p: ProductORM) -> Product:
    # category 现有 ORM 无独立列（存在 name / attributes 里），V3+ 拆列；此处留 None
    return Product(
        sku=p.sku,
        name=p.name,
        category=None,
        price=float(p.price),
        stock=p.stock,
        description=p.description,
        images=[],                    # 现有 ORM 无 images 列，接入方自定义实现可填
        attributes=p.attributes,      # 保留 ORM attributes（消费方依赖，行为兼容）
    )


# =============================================================
# OrderService 默认实现
# =============================================================
class MySQLOrderService:
    """OrderService Protocol 的 MySQL 默认实现"""

    async def get_order(self, user_id: int, order_no: str) -> Optional[Order]:
        with with_safe_session(commit=False) as db:
            o = db.query(OrderORM).filter(
                OrderORM.order_no == order_no,
                OrderORM.user_id == user_id,      # 越权防护
                OrderORM.deleted == 0,
            ).first()
            if not o:
                return None
            items = db.query(OrderItemORM).filter(
                OrderItemORM.order_id == o.id,
                OrderItemORM.deleted == 0,
            ).all()
            return _to_order(o, items)

    async def list_user_orders(
        self, user_id: int, status: Optional[str] = None,
        start_date: Optional[datetime] = None, end_date: Optional[datetime] = None,
        limit: int = 20, cursor: Optional[str] = None,
    ) -> tuple[List[Order], Optional[str]]:
        offset = _decode_cursor(cursor)
        with with_safe_session(commit=False) as db:
            q = db.query(OrderORM).filter(
                OrderORM.user_id == user_id,
                OrderORM.deleted == 0,
            )
            if status:
                q = q.filter(OrderORM.status == status)
            if start_date:
                q = q.filter(OrderORM.create_time >= start_date)
            if end_date:
                q = q.filter(OrderORM.create_time <= end_date)
            q = q.order_by(OrderORM.create_time.desc())
            # 多取 1 条判断是否还有下一页
            rows = q.offset(offset).limit(limit + 1).all()
            has_more = len(rows) > limit
            rows = rows[:limit]

            orders: List[Order] = []
            for o in rows:
                items = db.query(OrderItemORM).filter(
                    OrderItemORM.order_id == o.id,
                    OrderItemORM.deleted == 0,
                ).all()
                orders.append(_to_order(o, items))

            next_cursor = _encode_cursor(offset + limit) if has_more else None
            return orders, next_cursor

    async def get_order_status(self, order_no: str) -> Optional[str]:
        with with_safe_session(commit=False) as db:
            o = db.query(OrderORM).filter(
                OrderORM.order_no == order_no,
                OrderORM.deleted == 0,
            ).first()
            return o.status if o else None


# =============================================================
# ProductService 默认实现
# =============================================================
class MySQLProductService:
    """ProductService Protocol 的 MySQL 默认实现"""

    async def get_product(self, sku: str) -> Optional[Product]:
        with with_safe_session(commit=False) as db:
            p = db.query(ProductORM).filter(
                ProductORM.sku == sku,
                ProductORM.status == 1,
                ProductORM.deleted == 0,
            ).first()
            return _to_product(p) if p else None

    async def search_products(
        self, query: str, category: Optional[str] = None,
        limit: int = 10,
    ) -> List[Product]:
        with with_safe_session(commit=False) as db:
            q = db.query(ProductORM).filter(
                ProductORM.name.contains(query),
                ProductORM.status == 1,
                ProductORM.deleted == 0,
            )
            if category:
                # 类目词出现在 name 里（沿用 ProductTool.list_products 语义）
                q = q.filter(ProductORM.name.contains(category))
            products = q.order_by(ProductORM.id).limit(limit).all()
            return [_to_product(p) for p in products]

    async def get_recommendations(
        self, user_id: int, context_skus: List[str], limit: int = 5,
    ) -> List[Product]:
        # V2 简化：排除上下文已含 SKU，返回其他在售商品（按 id 稳定排序）
        # V3+ 替换为向量相似度 / 协同过滤（YAGNI：真营销场景出现再上）
        with with_safe_session(commit=False) as db:
            q = db.query(ProductORM).filter(
                ProductORM.status == 1,
                ProductORM.deleted == 0,
            )
            if context_skus:
                q = q.filter(ProductORM.sku.notin_(context_skus))
            products = q.order_by(ProductORM.id).limit(limit).all()
            return [_to_product(p) for p in products]


# =============================================================
# cursor 编解码（offset-based，简化实现）
# =============================================================
def _encode_cursor(offset: int) -> str:
    return str(offset)


def _decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except (ValueError, TypeError):
        return 0
