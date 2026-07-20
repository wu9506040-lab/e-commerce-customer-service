"""
业务领域 Schema — Order / OrderItem / Product / ProductQuery（Sprint 15）
                  Refund / Logistics / TrackingEvent / TrackingInfo（Sprint 16）

CLAUDE.md §9.3.1 五件套之「输入/输出模型」：DTO，不暴露 ORM。
OrderService / ProductService / RefundService / LogisticsService Protocol 的出入参
统一用这些 Pydantic 模型，接入方（自建订单/商品/退款/物流系统）实现 Protocol 时
按此 schema 返回即可。
"""
from datetime import datetime
from typing import Dict, List, Optional

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


# =============================================================
# Sprint 18 场景组 A · AfterSalesRuleService DTO
# （CLAUDE.md §9.3.1 五件套之「输入/输出模型」；DTO 不暴露 ORM）
# =============================================================
class RefundReasonAdvice(BaseModel):
    """退款原因填写指导"""
    order_no: str
    reason_category: str
    suggested_reason_text: str          # 建议填写的具体文字
    success_rate_hint: str              # "高" / "中" / "低"（基于 reason_category）
    evidence_required: List[str]        # 需要的凭证（照片/视频/聊天截图）
    additional_tips: List[str]          # 额外提示（如"不要写'不想要了'"）


class ShippingInsuranceInfo(BaseModel):
    """运费险规则"""
    order_no: str
    insured: bool                       # 该订单是否购买运费险
    coverage_amount: Optional[float] = None     # 赔付额度（最高 X 元）
    eligible: bool                      # 当前情况是否符合理赔条件
    estimated_payout_days: Optional[int] = None  # 预计到账天数
    notes: List[str]                    # 注意事项


class RefundTypeAdvice(BaseModel):
    """仅退款 vs 退货退款建议"""
    order_no: str
    recommended_type: str               # "refund_only" / "return_and_refund"
    reasoning: str                      # 推荐理由（中文）
    conditions: List[str]               # 适用条件列表


# =============================================================
# Sprint 18 · Promotion / CouponStackResult / BundleDiscountResult
# =============================================================
class Promotion(BaseModel):
    """优惠活动 DTO

    type 取值：full_reduction / discount / gift。
    applicable_stores / applicable_categories 为空列表 = 全店/全部类目。
    """
    promotion_id: str
    name: str
    type: str
    threshold: Optional[float] = None    # 满 X 触发（full_reduction 必填）
    benefit: Optional[float] = None      # 减 Y（full_reduction）/ 折扣率（discount）
    applicable_stores: List[str] = Field(default_factory=list)
    applicable_categories: List[str] = Field(default_factory=list)
    start_time: datetime
    end_time: datetime
    stackable: bool


class CouponStackResult(BaseModel):
    """优惠券叠加校验结果 DTO

    业务语义：哪些券能一起用 / 哪些互斥 / 推荐最佳组合（优惠最大化）。
    V2 仅给"建议"，最终叠加由用户在前端勾选。
    """
    stackable_groups: List[List[str]] = Field(default_factory=list)
    # 互斥的券对：[{"a": "C001", "b": "C002", "reason": "..."}]
    conflicting_pairs: List[dict] = Field(default_factory=list)
    best_combination: List[str] = Field(default_factory=list)


class BundleDiscountResult(BaseModel):
    """跨店满减计算结果 DTO

    输入每家店金额 → 输出当前合计 + 距离下一档满减还差多少 + 凑单建议。
    next_threshold=None 表示已达标最高档，无下一档建议。
    """
    current_total: float
    store_totals: Dict[str, float] = Field(default_factory=dict)
    next_threshold: Optional[float] = None
    next_benefit: Optional[float] = None
    suggestion: Optional[str] = None


# =============================================================
# Sprint 18-C · 售中订单修改（OrderModifyService）Schema
# =============================================================
class ModifyResult(BaseModel):
    """售中修改结果（地址/规格）。

    success=False 时 reason 包含中文失败原因；before_snapshot/after_snapshot
    为 None 时上层按需自补（便于审计回滚）。
    """
    success: bool
    order_no: str
    modification_type: str             # "address" / "spec"
    reason: str                        # 中文提示
    before_snapshot: Optional[dict] = None
    after_snapshot: Optional[dict] = None


class MergeResult(BaseModel):
    """合并订单结果。

    success=True 时 primary_order_no 为主订单号，merged_order_nos 为被合并订单号列表；
    success=False 时只返 reason。
    """
    success: bool
    primary_order_no: Optional[str] = None
    merged_order_nos: List[str] = Field(default_factory=list)
    reason: str


# =============================================================
# 售中订单修改 异常类（CLAUDE.md §9.3.1 五件套之「异常处理」）
# =============================================================
class ModifyError(Exception):
    """售中订单修改操作错误基类"""


class ModifyNotAllowedError(ModifyError):
    """订单状态不允许修改（已发货/已签收/已完成/已退款）"""


class OrderNotFoundError(ModifyError):
    """订单不存在或越权访问（user_id 不匹配）"""


class MergeConditionError(ModifyError):
    """合并订单条件不满足（跨店/已发货/超过 5 分钟等）"""
