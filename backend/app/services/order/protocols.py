"""
OrderService + ProductService Protocol（CLAUDE.md §9.3.2 支持模块替换）

接入方实现该接口即可对接自家订单/商品系统。
默认实现：MySQLImpl（基于现有 OrderTool/ProductTool 重构，见 mysql_impl.py）。

设计（CLAUDE.md §9.7 自检 5 问）：
- Q1 业务模块（Tool 层）依赖此 Protocol，不依赖具体实现
- Q3 Protocol 先于实现（本文件先定义签名）
- 方法全 async（spec §8 决策 #4；FastAPI 友好）
"""
from datetime import datetime
from typing import List, Optional, Protocol, runtime_checkable

from app.schemas.business import Order, Product


@runtime_checkable
class OrderService(Protocol):
    """订单服务协议"""

    async def get_order(self, user_id: int, order_no: str) -> Optional[Order]:
        """按订单号查询订单（含 items，强制 user_id 防越权）；不存在返 None"""
        ...

    async def list_user_orders(
        self, user_id: int, status: Optional[str] = None,
        start_date: Optional[datetime] = None, end_date: Optional[datetime] = None,
        limit: int = 20, cursor: Optional[str] = None,
    ) -> tuple[List[Order], Optional[str]]:
        """查询用户订单列表；返 (orders, next_cursor)。cursor 为下一页游标（无更多则 None）"""
        ...

    async def get_order_status(self, order_no: str) -> Optional[str]:
        """查订单当前状态（pending / paid / shipped / completed / cancelled）；不存在返 None"""
        ...


@runtime_checkable
class ProductService(Protocol):
    """商品服务协议"""

    async def get_product(self, sku: str) -> Optional[Product]:
        """按 SKU 查商品详情；不存在返 None"""
        ...

    async def search_products(
        self, query: str, category: Optional[str] = None,
        limit: int = 10,
    ) -> List[Product]:
        """关键词搜索商品（query + 可选分类）"""
        ...

    async def get_recommendations(
        self, user_id: int, context_skus: List[str], limit: int = 5,
    ) -> List[Product]:
        """基于上下文 SKU 推荐相似商品（主动营销用）"""
        ...


@runtime_checkable
class OrderServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_order_service(self) -> OrderService: ...
    def get_product_service(self) -> ProductService: ...


# === 异常类 ===
class OrderError(Exception): ...
class OrderNotFoundError(OrderError): ...
class ProductError(Exception): ...
class ProductNotFoundError(ProductError): ...
