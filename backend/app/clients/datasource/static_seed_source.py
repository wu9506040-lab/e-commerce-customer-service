"""
StaticSeedSource — 当前默认实现（读 MySQL 种子数据）

业务背景：
  - 项目当前阶段（M14 V3）：商品/订单全部从 MySQL 种子数据读取
  - V3.2 规划（M15+）：当 DataSource 接入 OrderService 时，本类替换现有 ProductTool/OrderTool 直连 MySQL

按 CLAUDE.md §9.3 Interface First：实现 DataSourceProtocol；不暴露 MySQL / ORM 细节。

YAGNI 边界（CLAUDE.md §3.3）：
  - 当前只实现 fetch_* + search_products_by_keyword 5 个方法
  - subscribe_webhook 抛 NotImplementedError（占位；M18+ 真接入淘宝时由 TaobaoAdapter 实现）
"""
import logging
from typing import AsyncIterator, List, Optional

from app.clients.datasource.protocols import DataSourceProtocol
from app.clients.mysql_client import with_safe_session
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User

logger = logging.getLogger(__name__)


class StaticSeedSource(DataSourceProtocol):
    """
    种子数据源（MySQL 实现）

    复用现有 ORM 模型（Product / Order / OrderItem / User）；
    调用方拿到的是 dict（不暴露 ORM 细节）；
    Session 通过 with_safe_session 隔离（与 ingest 流水线一致）。

    Usage:
        source: DataSourceProtocol = StaticSeedSource()
        products = source.fetch_products(category="手机")
        orders = source.fetch_orders(user_id=42)
    """

    # =============================================================
    # 商品
    # =============================================================

    @staticmethod
    def fetch_products(
        *,
        sku: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """
        商品查询

        复用 ProductTool 实现逻辑（M14 V3 期间 ProductTool 是 OrderService 入口；
        M15+ 切到 DataSource 时此方法成为唯一入口）
        """
        with with_safe_session(commit=False) as db:
            q = db.query(Product).filter(Product.status == 1, Product.deleted == 0)
            if sku:
                q = q.filter(Product.sku == sku)
            if category:
                # 简化：商品 name LIKE 匹配类目词
                q = q.filter(Product.name.contains(category))
            products = q.order_by(Product.id).limit(limit).all()
            return [StaticSeedSource._product_to_dict(p) for p in products]

    @staticmethod
    def search_products_by_keyword(keyword: str, *, limit: int = 10) -> List[dict]:
        """商品关键词搜索"""
        with with_safe_session(commit=False) as db:
            q = (
                db.query(Product)
                .filter(Product.status == 1, Product.deleted == 0)
                .filter(Product.name.contains(keyword))
                .order_by(Product.id)
                .limit(limit)
            )
            return [StaticSeedSource._product_to_dict(p) for p in q.all()]

    # =============================================================
    # 订单
    # =============================================================

    @staticmethod
    def fetch_orders(
        *,
        user_id: int,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """
        用户订单列表（user_id 显式必传，防越权 — 与 OrderTool 一致）
        """
        with with_safe_session(commit=False) as db:
            q = db.query(Order).filter(Order.user_id == user_id, Order.deleted == 0)
            if status:
                q = q.filter(Order.status == status)
            orders = q.order_by(Order.create_time.desc()).limit(limit).all()
            return [StaticSeedSource._order_to_dict(o) for o in orders]

    @staticmethod
    def fetch_user_orders_with_logistics(
        *,
        user_id: int,
        order_no: str,
    ) -> Optional[dict]:
        """订单详情 + 明细 + 物流 mock"""
        with with_safe_session(commit=False) as db:
            order = (
                db.query(Order)
                .filter(
                    Order.order_no == order_no,
                    Order.user_id == user_id,
                    Order.deleted == 0,
                )
                .first()
            )
            if not order:
                return None

            items = (
                db.query(OrderItem)
                .filter(OrderItem.order_id == order.id, OrderItem.deleted == 0)
                .all()
            )

            logistics = StaticSeedSource._mock_logistics(order)

            return {
                "order": StaticSeedSource._order_to_dict(order),
                "items": [StaticSeedSource._item_to_dict(i) for i in items],
                "logistics": logistics,
            }

    # =============================================================
    # 用户
    # =============================================================

    @staticmethod
    def fetch_user_profile(*, user_id: int) -> Optional[dict]:
        """用户档案（不暴露 password_hash）"""
        with with_safe_session(commit=False) as db:
            u = (
                db.query(User)
                .filter(User.id == user_id, User.deleted == 0)
                .first()
            )
            if not u:
                return None
            return {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "email": u.email,
                "phone": u.phone,
                "role": u.role,
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "create_time": u.create_time.isoformat() if u.create_time else None,
            }

    # =============================================================
    # 自更新（M18+ 占位）
    # =============================================================

    @staticmethod
    async def subscribe_webhook(event_type: str) -> AsyncIterator[dict]:
        """
        占位 — M18+ 接 TaobaoAdapter 时由 TaobaoAdapter 实现

        当前实现不抛错而抛 NotImplementedError：
            让业务层明确知道"此数据源不支持 webhook 订阅"
            （避免误以为订阅成功但实际无事件）
        """
        raise NotImplementedError(
            f"StaticSeedSource 不支持 webhook 订阅（event_type={event_type}）。"
            "M18+ 接入 TaobaoAdapter 时由 TaobaoAdapter 提供此能力。"
        )
        # 让 mypy/IDE 知道这是 AsyncIterator，raise 后不返回
        yield  # pragma: no cover

    # =============================================================
    # 私有：ORM → dict
    # =============================================================

    @staticmethod
    def _product_to_dict(p: Product) -> dict:
        """商品 ORM → dict（不暴露 ORM 细节）"""
        # category 在 attributes JSON 里（schema 不直接列；M1 设计）
        attrs = p.attributes if p.attributes else {}
        return {
            "sku": p.sku,
            "name": p.name,
            "price": float(p.price) if p.price is not None else 0.0,
            "category": attrs.get("category") if isinstance(attrs, dict) else None,
            "stock": p.stock if p.stock is not None else 0,
            "status": p.status,
            "attributes": attrs if isinstance(attrs, dict) else None,
        }

    @staticmethod
    def _order_to_dict(o: Order) -> dict:
        return {
            "order_no": o.order_no,
            "status": o.status,
            "total_amount": float(o.total_amount),
            "create_time": o.create_time.isoformat() if o.create_time else None,
        }

    @staticmethod
    def _item_to_dict(i: OrderItem) -> dict:
        return {
            "sku": i.sku,
            "product_name": i.product_name,
            "qty": i.qty,
            "unit_price": float(i.unit_price),
            "subtotal": float(i.subtotal),
        }

    @staticmethod
    def _mock_logistics(order: Order) -> dict:
        """物流 mock（M14 V3 仍为 mock；M15+ 接顺丰/京东物流 API）"""
        if order.status in ("pending", "paid"):
            return {
                "order_no": order.order_no,
                "status": "待发货",
                "last_location": "仓库分拣中",
                "trajectory": [{"time": order.create_time.isoformat(), "location": "订单已创建"}],
            }
        return {
            "order_no": order.order_no,
            "status": "已发货",
            "last_location": "上海青浦转运中心",
            "trajectory": [
                {"time": order.create_time.isoformat(), "location": "订单已创建"},
                {"time": order.create_time.isoformat(), "location": "已发往上海青浦转运中心"},
            ],
        }
