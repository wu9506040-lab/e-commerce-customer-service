"""
商品 / 订单公开 API 的 Pydantic schema

设计：
- cover_url 用相对路径 /products/{sku}.jpg（前端 public 目录）
- 订单详情聚合 items + logistics
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


# =============================================================
# 商品
# =============================================================
class ProductOut(BaseModel):
    """单个商品（公开）"""
    sku: str
    name: str
    price: float
    stock: int
    attributes: Optional[dict[str, Any]] = None
    description: Optional[str] = None
    cover_url: str = Field(..., description="相对路径，前端拼接 base URL")


class ProductListResponse(BaseModel):
    """商品列表响应"""
    products: list[ProductOut]
    total: int


# =============================================================
# 订单
# =============================================================
class OrderItemOut(BaseModel):
    """订单明细项"""
    sku: str
    product_name: str
    qty: int
    unit_price: float
    subtotal: float


class LogisticsOut(BaseModel):
    """物流信息"""
    order_no: str
    logistics_no: Optional[str] = None
    status: str
    last_location: Optional[str] = None
    trajectory: list[dict[str, Any]] = []


class OrderSummaryOut(BaseModel):
    """订单概要（列表用）"""
    order_no: str
    status: str
    total_amount: float
    create_time: Optional[str] = None
    item_count: int = Field(0, description="商品行数（item_count）")


class OrderListResponse(BaseModel):
    """订单列表响应"""
    orders: list[OrderSummaryOut]
    total: int


class OrderDetailOut(BaseModel):
    """订单详情（聚合主表 + items + 物流）"""
    order: OrderSummaryOut
    items: list[OrderItemOut] = []
    logistics: Optional[LogisticsOut] = None
