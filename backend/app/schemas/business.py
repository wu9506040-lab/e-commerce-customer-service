"""
业务领域 Schema — Order / OrderItem / Product / ProductQuery（Sprint 15）
                  Refund / Logistics / TrackingEvent / TrackingInfo（Sprint 16）

CLAUDE.md §9.3.1 五件套之「输入/输出模型」：DTO，不暴露 ORM。
OrderService / ProductService / RefundService / LogisticsService Protocol 的出入参
统一用这些 Pydantic 模型，接入方（自建订单/商品/退款/物流系统）实现 Protocol 时
按此 schema 返回即可。
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    """订单明细行（DTO；对应 order_items 表冗余字段）"""
    sku: str
    product_name: str
    quantity: int
    unit_price: float
    subtotal: float


class Order(BaseModel):
    """订单 DTO（含 items）

    status 取值：pending / paid / shipped / delivered / completed
    / cancelled / refunding / refunded（接入方可扩展，AI 客服按字符串消费）。
    """
    order_no: str
    user_id: int
    status: str
    items: List[OrderItem] = Field(default_factory=list)
    total_amount: float
    shipping_address: Optional[str] = None
    tracking_no: Optional[str] = None
    create_time: datetime
    update_time: datetime


class Product(BaseModel):
    """商品 DTO"""
    sku: str
    name: str
    category: Optional[str] = None
    price: float
    stock: int
    description: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    # spec §2.3 之外的加性字段（Optional 默认 None）：现有 ProductTool 消费方
    # （shop.py / orchestrator / prompt_assembler）依赖 attributes，保留以维持行为兼容。
    attributes: Optional[dict] = None


class ProductQuery(BaseModel):
    """商品搜索入参（关键词 + 可选分类）"""
    query: str = Field(..., min_length=1, max_length=200)
    category: Optional[str] = None
    limit: int = Field(10, ge=1, le=50)


# =============================================================
# Sprint 16 · Refund / Logistics / Tracking
# =============================================================
class Refund(BaseModel):
    """退款 DTO

    status 取值：pending / approved / rejected / completed（接入方可扩展）。
    order_no：关联订单号（get_refund / list_user_refunds 注入；get_refund_status 不注入）。
    """
    refund_no: str
    order_no: Optional[str] = None
    user_id: int
    status: str
    amount: float
    reason: str
    remark: Optional[str] = None
    create_time: datetime
    update_time: datetime


class TrackingEvent(BaseModel):
    """物流轨迹单事件"""
    time: datetime
    event: str                           # 已下单 / 已发货 / 运输中 / 已签收 / 已退回
    location: Optional[str] = None


class Logistics(BaseModel):
    """物流汇总（query 返回）"""
    order_no: str
    tracking_no: Optional[str] = None
    carrier: Optional[str] = None        # 顺丰 / 中通 / etc
    status: str                          # 待发货 / 运输中 / 已签收 / 已退回 / 未知
    last_location: Optional[str] = None
    estimated_arrival: Optional[datetime] = None


class TrackingInfo(BaseModel):
    """物流轨迹详情（track 返回）"""
    tracking_no: str
    carrier: str
    status: str
    events: List[TrackingEvent] = Field(default_factory=list)
