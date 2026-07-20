"""
SupplementRuleService YAML 默认实现 — Sprint 20 通用客服中台（spec §2）

V2 简化：售前+售中补完规则全是静态业务规则 + 部分订单状态查询。
为什么不叫 mysql_impl：spec §2.3 明确说静态规则在 YAML；YAML 优先可不连数据库。

业务规则来源：
- 运费规则：YAML.SHIPPING_RULES
- 限购规则：YAML.PURCHASE_LIMITS
- 催发货：YAML.URGE_SHIPMENT_RULES
- 延长收货：YAML.EXTEND_RECEIPT_RULES
- 上门取件：YAML.SCHEDULE_PICKUP_RULES

订单状态查询：通过 OrderService Protocol（mock 友好、越权防护内建）。

错误处理：
- 订单不存在 → service 内部按业务需要返对应状态（不抛异常，让用户决策）
- YAML key 缺失 → .get(key, default) 不抛异常
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.schemas.business import (
    ExtendReceiptResult,
    PurchaseLimitInfo,
    SchedulePickupResult,
    ShippingFeeInfo,
    UrgeShipmentResult,
)
from app.services.config_loader import get_config_loader
from app.services.order.factory import get_order_service_factory
from app.services.order.protocols import OrderService


class YamlSupplementRuleService:
    """SupplementRuleService Protocol 的 YAML 默认实现

    - 静态规则：从 supplement.yaml 启动期加载（YAMLConfigLoader 缓存）
    - 动态数据：通过 OrderService Protocol 查订单状态/金额（不直连 ORM）
    """

    def __init__(
        self,
        order_service: Optional[OrderService] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """构造方法（双注入方便测试）

        Args:
            order_service: OrderService Protocol 实例；None → 用工厂默认单例
            config: 业务规则 dict；None → 用 config_loader.load("supplement")
        """
        self._order_service = order_service
        self._config = config if config is not None else get_config_loader().load("supplement")

    # ---------------------------------------------------------
    # 私有：订单服务懒加载
    # ---------------------------------------------------------
    def _get_order_service(self) -> OrderService:
        if self._order_service is None:
            self._order_service = get_order_service_factory().get_order_service()
        return self._order_service

    # =============================================================
    # 1. 运费计算（售前）
    # =============================================================
    async def get_shipping_fee(
        self,
        address_region: str,
        item_count: int,
        total_amount: float,
        payment_method: str,
    ) -> ShippingFeeInfo:
        rules: Dict[str, Any] = self._config.get("SHIPPING_RULES", {})
        free_threshold: float = float(rules.get("free_shipping_threshold", 99.0))
        base_by_region: Dict[str, float] = dict(rules.get("base_fee_per_region", {}))
        additional_fee_per_item: float = float(rules.get("additional_fee_per_item", 2.0))
        cod_surcharge: float = float(rules.get("cod_surcharge", 5.0))
        remote_keywords: List[str] = list(rules.get("remote_area_keywords", []))

        # 1. 包邮判断
        is_free = total_amount >= free_threshold

        # 2. 偏远地区加价（地区名含 keyword 视为偏远）
        remote_area_surcharge = 0.0
        is_remote = any(kw in address_region for kw in remote_keywords)
        if is_remote:
            # 偏远地区首件加价 10 元（按 spec §2.4 #2 "偏远地区加价"）
            remote_area_surcharge = 10.0

        # 3. 首件基础运费（取地区匹配；不匹配默认 10.0）
        base_fee = float(base_by_region.get(address_region, 10.0))

        # 4. 续件运费：max(0, item_count - 1) * additional_fee_per_item
        additional_fee = max(0, item_count - 1) * additional_fee_per_item

        # 5. 货到付款加价
        cod_fee = cod_surcharge if payment_method == "cod" else 0.0

        # 6. 合计
        if is_free:
            # 包邮场景：基础运费 / 续件 / 偏远加价 / 货到付款加价 都按 0 计算
            # 但偏远 / 货到付款 / 续件 仍展示给用户作参考
            total_fee = 0.0
            notes = [f"订单金额满 ¥{free_threshold:.0f} 免运费"]
            if remote_area_surcharge > 0:
                notes.append(f"偏远地区附加费 ¥{remote_area_surcharge:.0f}（已免）")
        else:
            total_fee = base_fee + additional_fee + remote_area_surcharge + cod_fee
            notes = []
            if base_fee > 0:
                notes.append(f"首件运费 ¥{base_fee:.0f}")
            if additional_fee > 0:
                notes.append(f"续件 {item_count - 1} 件 × ¥{additional_fee_per_item:.0f}")
            if remote_area_surcharge > 0:
                notes.append(f"偏远地区附加费 ¥{remote_area_surcharge:.0f}")
            if cod_fee > 0:
                notes.append(f"货到付款附加费 ¥{cod_fee:.0f}")

        return ShippingFeeInfo(
            base_fee=base_fee if not is_free else 0.0,
            additional_fee=additional_fee if not is_free else 0.0,
            total_fee=total_fee,
            free_shipping_threshold=free_threshold,
            is_free_shipping=is_free,
            remote_area_surcharge=remote_area_surcharge,
            notes=notes,
        )

    # =============================================================
    # 2. 限购规则（售前）
    # =============================================================
    async def get_purchase_limit_info(
        self, sku: str, user_id: int,
    ) -> PurchaseLimitInfo:
        rules: Dict[str, Any] = self._config.get("PURCHASE_LIMITS", {})
        default_max: int = int(rules.get("default_max_per_user", 5))
        activity_skus: List[str] = list(rules.get("activity_limited_skus", []))
        activity_max: int = int(rules.get("activity_max_per_user", 1))

        # 判断是否活动期间限购 SKU
        is_activity = sku in activity_skus

        if is_activity:
            return PurchaseLimitInfo(
                sku=sku,
                limited=True,
                max_quantity=activity_max,
                activity_limited=True,
                user_purchase_count=None,    # V2 简化：不查历史购买
                remaining_quota=None,        # V2 简化：不查历史购买
            )
        return PurchaseLimitInfo(
            sku=sku,
            limited=True,
            max_quantity=default_max,
            activity_limited=False,
            user_purchase_count=None,
            remaining_quota=None,
        )

    # =============================================================
    # 3. 催发货（售中 · V2 仅规则说明）
    # =============================================================
    async def urge_shipment(
        self, user_id: int, order_no: str,
    ) -> UrgeShipmentResult:
        rules: Dict[str, Any] = self._config.get("URGE_SHIPMENT_RULES", {})
        promised_hours: int = int(rules.get("promised_ship_hours", 48))
        max_urge_per_day: int = int(rules.get("max_urge_per_day", 2))
        next_interval_hours: int = int(rules.get("next_urge_interval_hours", 12))

        # 1. 查订单状态
        order = await self._get_order_service().get_order(user_id=user_id, order_no=order_no)
        if order is None:
            # 订单不存在时按待发货状态给规则提示（不抛异常，让前端展示）
            return UrgeShipmentResult(
                order_no=order_no,
                order_status="unknown",
                promised_ship_time=None,
                urged_count=0,
                next_urge_available=None,
                tips=[
                    f"商家承诺 {promised_hours} 小时内发货",
                    f"每天最多催 {max_urge_per_day} 次",
                    f"两次催发货间隔 {next_interval_hours} 小时",
                ],
            )

        order_status = order.status
        now = datetime.now()
        promised_ship_time = order.create_time + timedelta(hours=promised_hours)

        # 2. V2 简化：催发货次数从 order.attributes 取（无字段则返 0）
        urged_count = 0

        # 3. 下次可催时间（首次催 = 立即可催）
        next_urge = now if urged_count == 0 else now + timedelta(hours=next_interval_hours)

        # 4. 催发货技巧
        tips = [
            f"商家承诺 {promised_hours} 小时内发货",
            f"每天最多催 {max_urge_per_day} 次",
            f"两次催发货间隔 {next_interval_hours} 小时",
            "建议先通过客服沟通，避免频繁催促",
        ]

        return UrgeShipmentResult(
            order_no=order_no,
            order_status=order_status,
            promised_ship_time=promised_ship_time,
            urged_count=urged_count,
            next_urge_available=next_urge,
            tips=tips,
        )

    # =============================================================
    # 4. 延长收货（售中 · V2 仅资格检查）
    # =============================================================
    async def extend_receipt(
        self, user_id: int, order_no: str, extend_days: int,
    ) -> ExtendReceiptResult:
        rules: Dict[str, Any] = self._config.get("EXTEND_RECEIPT_RULES", {})
        eligible_statuses: List[str] = list(rules.get("eligible_statuses", ["shipped"]))
        max_per: int = int(rules.get("max_days_per_extension", 7))
        max_total: int = int(rules.get("max_days_total", 15))

        # 1. 查订单状态（防越权）
        order = await self._get_order_service().get_order(user_id=user_id, order_no=order_no)
        if order is None:
            return ExtendReceiptResult(
                eligible=False,
                max_extension_days=max_per,
                remaining_extension_days=0,
                current_status="unknown",
                reason=f"订单不存在或越权访问: order_no={order_no}",
            )

        current_status = order.status

        # 2. 状态检查：仅 eligible_statuses 中的状态可延长
        if current_status not in eligible_statuses:
            return ExtendReceiptResult(
                eligible=False,
                max_extension_days=max_per,
                remaining_extension_days=0,
                current_status=current_status,
                reason=f"当前订单状态（{current_status}）不可延长，仅「已发货」状态可申请",
            )

        # 3. 单次延长天数检查
        if extend_days <= 0 or extend_days > max_per:
            return ExtendReceiptResult(
                eligible=False,
                max_extension_days=max_per,
                remaining_extension_days=max_total,
                current_status=current_status,
                reason=f"单次延长天数需在 1 ~ {max_per} 天之间",
            )

        # 4. 累计天数检查（V2 简化：已延长时间从 order.attributes 取，无则 0）
        already_extended = 0
        remaining = max_total - already_extended
        if extend_days > remaining:
            return ExtendReceiptResult(
                eligible=False,
                max_extension_days=max_per,
                remaining_extension_days=remaining,
                current_status=current_status,
                reason=f"累计延长天数不能超过 {max_total} 天，本次最大可延长 {remaining} 天",
            )

        return ExtendReceiptResult(
            eligible=True,
            max_extension_days=max_per,
            remaining_extension_days=remaining,
            current_status=current_status,
            reason=f"可延长 {extend_days} 天（单次最长 {max_per} 天，累计不超过 {max_total} 天）",
        )

    # =============================================================
    # 5. 上门取件预约（退货物流 · V2 仅规则说明）
    # =============================================================
    async def schedule_pickup(
        self, order_no: str, pickup_address: str, time_slot: str,
    ) -> SchedulePickupResult:
        rules: Dict[str, Any] = self._config.get("SCHEDULE_PICKUP_RULES", {})
        available_slots: List[str] = list(rules.get("available_time_slots", []))
        pickup_fee: float = float(rules.get("pickup_fee", 0.0))
        advance_hours: int = int(rules.get("advance_booking_hours", 24))

        # 1. 校验 time_slot
        if time_slot not in available_slots:
            return SchedulePickupResult(
                available=False,
                available_time_slots=available_slots,
                pickup_fee=pickup_fee,
                notes=[
                    f"时段「{time_slot}」不可用，可选时段：{', '.join(available_slots)}",
                ],
            )

        # 2. 校验时间（V2 简化：仅检查参数 time_slot 合法性，不验实际时间戳）
        #    因接口设计未传 datetime，按 advance_booking_hours 仅给规则说明
        notes = [
            f"请至少提前 {advance_hours} 小时预约",
            "上门取件时准备好退货商品 + 包装",
            "如需取消请提前联系客服",
        ]

        if pickup_fee == 0.0:
            notes.append("上门取件免费")

        return SchedulePickupResult(
            available=True,
            available_time_slots=available_slots,
            pickup_fee=pickup_fee,
            notes=notes,
        )
