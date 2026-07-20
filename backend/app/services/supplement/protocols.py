"""
SupplementRuleService Protocol（CLAUDE.md §9.3.2 支持模块替换）

V2 范围：纯规则查询，不调 LLM，不写订单。
业务规则从 config/business_rules/supplement.yaml 加载（§9.4.2）。
默认实现：YamlSupplementRuleService（静态规则 + OrderService 查询，越权防护）。

5 个场景：运费 / 限购 / 催发货 / 延长收货 / 上门取件预约。

设计（CLAUDE.md §9.7 自检 5 问）：
- Q1 业务模块依赖此 Protocol，不依赖具体实现
- Q3 Protocol 先于实现（本文件先定义签名）
- 方法全 async（spec §8 决策 #4；FastAPI 友好）

接入方：自建业务中台 / 客服系统实现此接口即可对接 AI 客服售前+售中补完咨询。
"""
from typing import List, Optional, Protocol, runtime_checkable

from app.schemas.business import (
    ExtendReceiptResult,
    PurchaseLimitInfo,
    SchedulePickupResult,
    ShippingFeeInfo,
    SupplementError,
    UrgeShipmentResult,
)


@runtime_checkable
class SupplementRuleService(Protocol):
    """售前+售中补完规则协议"""

    # ===== 售前补完 =====

    async def get_shipping_fee(
        self,
        address_region: str,
        item_count: int,
        total_amount: float,
        payment_method: str,
    ) -> ShippingFeeInfo:
        """运费计算

        address_region: "华北"/"华东"/"华南"/"西部"/"东北"/"偏远"
        item_count: 商品件数（>0）
        total_amount: 订单金额（>=0）
        payment_method: "online"/"cod"（货到付款）

        规则：满 X 元包邮 / 首件 Y 元 / 续件 Z 元 / 偏远地区加价。
        """
        ...

    async def get_purchase_limit_info(
        self, sku: str, user_id: int,
    ) -> PurchaseLimitInfo:
        """限购规则

        返回：该商品是否限购 / 限购数量 / 活动期间限购 / 历史购买次数。
        V2 简化：user_purchase_count / remaining_quota 返 None（依赖具体业务后端）；
        只要 YAML 规则足够给出 limited / max_quantity。
        """
        ...

    # ===== 售中补完 =====

    async def urge_shipment(
        self, user_id: int, order_no: str,
    ) -> UrgeShipmentResult:
        """催发货（V2 仅返规则说明，不实际催单）

        规则：商家承诺发货时间 / 已催过几次 / 下次可催时间。
        实际催发货 = 写操作；V2 仅返"催发货规则 + 建议"。
        """
        ...

    async def extend_receipt(
        self, user_id: int, order_no: str, extend_days: int,
    ) -> ExtendReceiptResult:
        """延长收货时间（V2 仅资格检查 + 规则说明）

        规则：仅"已发货未确认"可延长 / 单次最长 7 天 / 累计不超过 15 天。
        V2 仅返资格检查，不实际延长收货时间。
        """
        ...

    async def schedule_pickup(
        self, order_no: str, pickup_address: str, time_slot: str,
    ) -> SchedulePickupResult:
        """上门取件预约（退货物流，V2 仅返规则说明）

        time_slot: "morning" / "afternoon" / "evening"
        V2 仅返可预约时段 + 注意事项，不实际预约。
        """
        ...


@runtime_checkable
class SupplementRuleServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_supplement_rule_service(self) -> SupplementRuleService: ...


# === 异常类（就近定义，避免污染业务模块导出） ===
# 注：SupplementError 已定义在 app.schemas.business（统一管理业务异常基类）
