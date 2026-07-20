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


# =============================================================
# Sprint 20 · SupplementRuleService DTO（spec §2.2）
# 售前+售中补完：运费 / 限购 / 催发货 / 延长收货 / 上门取件
# =============================================================
class ShippingFeeInfo(BaseModel):
    """运费计算结果"""
    base_fee: float                         # 首件运费
    additional_fee: float                   # 续件运费
    total_fee: float                        # 合计运费
    free_shipping_threshold: float          # 包邮门槛
    is_free_shipping: bool                  # 是否包邮
    remote_area_surcharge: float            # 偏远地区加价
    notes: List[str]                        # 备注


class PurchaseLimitInfo(BaseModel):
    """限购规则"""
    sku: str
    limited: bool                           # 是否限购
    max_quantity: Optional[int] = None      # 限购数量
    activity_limited: bool                  # 活动期间限购
    user_purchase_count: Optional[int] = None  # 用户已购次数
    remaining_quota: Optional[int] = None  # 剩余可购


class UrgeShipmentResult(BaseModel):
    """催发货结果（V2 仅规则说明）"""
    order_no: str
    order_status: str                       # 订单当前状态
    promised_ship_time: Optional[datetime] = None  # 商家承诺发货时间
    urged_count: int = 0                    # 已催次数
    next_urge_available: Optional[datetime] = None  # 下次可催时间
    tips: List[str] = Field(default_factory=list)   # 催发货技巧


class ExtendReceiptResult(BaseModel):
    """延长收货结果（V2 仅资格检查）"""
    eligible: bool                          # 是否可延长
    max_extension_days: int                 # 单次最长可延长
    remaining_extension_days: int           # 剩余可延长天数
    current_status: str                     # 订单当前状态
    reason: str                             # 中文原因（不可延长时填）


class SchedulePickupResult(BaseModel):
    """上门取件预约结果"""
    available: bool
    available_time_slots: List[str] = Field(default_factory=list)  # 可预约时段
    pickup_fee: float                       # 上门取件费
    notes: List[str] = Field(default_factory=list)                  # 注意事项


# =============================================================
# Sprint 20 · DisputeService DTO（spec §3.2）
# 售后纠纷：质量问题鉴定 / 平台介入 / 举报假货
# =============================================================
class QualityDisputeProcess(BaseModel):
    """质量问题鉴定流程"""
    order_no: str
    burden_of_proof: str                    # "buyer" / "seller"
    evidence_required: List[str] = Field(default_factory=list)
    evidence_deadline_hours: int
    process_steps: List[str] = Field(default_factory=list)
    appeal_channels: List[str] = Field(default_factory=list)


class PlatformInterveneCheck(BaseModel):
    """平台介入条件检查"""
    eligible: bool
    order_no: str
    dispute_type: str
    reason: str                             # 中文原因
    required_conditions: List[str] = Field(default_factory=list)
    consequences: List[str] = Field(default_factory=list)


class ReportFakeGoodsProcess(BaseModel):
    """举报假货流程"""
    order_no: str
    report_channels: List[str] = Field(default_factory=list)
    evidence_required: List[str] = Field(default_factory=list)
    possible_penalties: List[str] = Field(default_factory=list)
    processing_days: int
    notes: List[str] = Field(default_factory=list)


# =============================================================
# Sprint 20 · InvoiceService DTO（spec §4.2）
# 发票：申请资格检查
# =============================================================
class InvoiceEligibility(BaseModel):
    """发票申请资格"""
    eligible: bool
    order_no: str
    invoice_type: str
    invoice_title_required: bool            # 是否需要填抬头
    tax_id_required: bool                  # 是否需要税号
    amount_threshold_met: bool             # 是否满足满额条件
    minimum_amount: float                  # 最低开票金额
    current_order_amount: float            # 当前订单金额
    eligible_amount: float                 # 可开票金额
    application_url: str                   # 申请入口 URL
    notes: List[str] = Field(default_factory=list)


# =============================================================
# Sprint 20 · 异常类（CLAUDE.md §9.3.1 五件套之「异常处理」）
# =============================================================
class SupplementError(Exception):
    """售前+售中补完规则服务基类异常"""


class DisputeError(Exception):
    """售后纠纷服务基类异常"""


class InvoiceError(Exception):
    """发票服务基类异常"""


class InvoiceNotEligibleError(InvoiceError):
    """发票申请资格不满足（金额不足/订单状态不符等）"""
