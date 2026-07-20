"""
AfterSalesRuleService YAML 默认实现 — Sprint 18 场景组 A（spec §2.3）

V2 简化：售后规则主要是静态业务规则 + 订单金额查询，不查数据库表。
为什么不叫 mysql_impl：spec §2.3 明确说售后规则是静态规则，主要逻辑在 YAML。

业务规则来源：
- 退款原因模板：YAML.REFUND_REASON_TEMPLATES
- 运费险规则：YAML.SHIPPING_INSURANCE
- 仅退款 vs 退货退款：YAML.REFUND_TYPE_RULES

订单金额查询：通过 OrderService Protocol（mock 友好、越权防护内建）。

错误处理（spec §2.5 #8）：
- 订单不存在 → 抛 OrderNotFoundForAdviceError
- YAML key 缺失 → .get(key, default) 不抛异常
- 未知的 reason_category → fallback 到 other
"""
from typing import Any, Dict, List, Optional

from app.schemas.business import (
    RefundReasonAdvice,
    RefundTypeAdvice,
    ShippingInsuranceInfo,
)
from app.services.config_loader import get_config_loader
from app.services.order.factory import get_order_service_factory
from app.services.order.protocols import OrderService


class YamlAfterSalesRuleService:
    """AfterSalesRuleService Protocol 的 YAML 默认实现

    - 静态规则：从 after_sales.yaml 启动期加载（YAMLConfigLoader 缓存）
    - 动态数据：通过 OrderService Protocol 查订单金额（不直连 ORM）
    """

    def __init__(
        self,
        order_service: Optional[OrderService] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """构造方法（双注入方便测试）

        Args:
            order_service: OrderService Protocol 实例；None → 用工厂默认单例
            config: 业务规则 dict；None → 用 config_loader.load("after_sales")
        """
        self._order_service = order_service
        self._config = config if config is not None else get_config_loader().load("after_sales")

    # ---------------------------------------------------------
    # 私有：订单服务懒加载
    # ---------------------------------------------------------
    def _get_order_service(self) -> OrderService:
        if self._order_service is None:
            self._order_service = get_order_service_factory().get_order_service()
        return self._order_service

    # =============================================================
    # 1. 退款原因填写指导
    # =============================================================
    async def get_refund_reason_advice(
        self, user_id: int, order_no: str, reason_category: str,
    ) -> RefundReasonAdvice:
        templates: Dict[str, Any] = self._config.get("REFUND_REASON_TEMPLATES", {})
        # 未知的 reason_category → fallback 到 other（spec §2.5 #8）
        template = templates.get(reason_category)
        if template is None:
            template = templates.get("other", {})

        return RefundReasonAdvice(
            order_no=order_no,
            reason_category=reason_category,
            suggested_reason_text=template.get("suggested_text", "其他原因"),
            success_rate_hint=template.get("success_rate", "低"),
            evidence_required=list(template.get("evidence_required", [])),
            additional_tips=list(template.get("additional_tips", [])),
        )

    # =============================================================
    # 2. 运费险规则
    # =============================================================
    async def get_shipping_insurance_info(
        self, order_no: str, return_status: str,
    ) -> ShippingInsuranceInfo:
        ins_cfg: Dict[str, Any] = self._config.get("SHIPPING_INSURANCE", {})
        eligible_statuses: List[str] = list(ins_cfg.get("eligible_statuses", []))
        estimated_payout_days: Optional[int] = ins_cfg.get("estimated_payout_days")
        default_coverage: float = float(ins_cfg.get("default_coverage", 25.0))
        high_value_threshold: float = float(ins_cfg.get("high_value_threshold", 200.0))
        high_value_coverage: float = float(ins_cfg.get("high_value_coverage", 50.0))

        # 1. 查 OrderService 拿订单金额（V2 简化：订单金额 > high_value_threshold → 高赔付）
        order = await self._get_order_service().get_order(user_id=0, order_no=order_no)
        # 注：user_id=0 表示不校验 user_id（运费险查询仅依赖 order_no；spec 未要求越权）

        # 2. 判断是否购买运费险（V2 简化：mock 字段从 attributes 取；接 ORM 时可加列）
        #    当前 ORM 无 shipping_insurance 字段 → 默认按「所有订单都购买」处理（电商常见）
        insured = True
        if order and order.items:
            first_item_attrs = getattr(order.items[0], "attributes", None) or {}
            if isinstance(first_item_attrs, dict):
                # 若 attributes 显式标注 shipping_insurance=false 则不购买
                if first_item_attrs.get("shipping_insurance") is False:
                    insured = False

        # 3. 判断是否符合当前阶段
        eligible = return_status in eligible_statuses

        # 4. 计算赔付额度
        coverage_amount: Optional[float] = None
        if insured and order is not None:
            if order.total_amount > high_value_threshold:
                coverage_amount = high_value_coverage
            else:
                coverage_amount = default_coverage

        # 5. 备注
        notes: List[str] = []
        if not insured:
            notes.append("该订单未购买运费险")
        if return_status not in eligible_statuses:
            notes.append(f"当前退货阶段（{return_status}）暂未到达理赔节点")

        return ShippingInsuranceInfo(
            order_no=order_no,
            insured=insured,
            coverage_amount=coverage_amount,
            eligible=eligible,
            estimated_payout_days=estimated_payout_days if eligible else None,
            notes=notes,
        )

    # =============================================================
    # 3. 仅退款 vs 退货退款推荐
    # =============================================================
    async def get_refund_type_advice(
        self, user_id: int, order_no: str,
    ) -> RefundTypeAdvice:
        rules: Dict[str, Any] = self._config.get("REFUND_TYPE_RULES", {})
        low_value_threshold: float = float(rules.get("low_value_threshold", 50.0))
        refund_only_scenarios: List[Dict[str, Any]] = list(rules.get("refund_only_scenarios", []))
        return_and_refund_scenarios: List[Dict[str, Any]] = list(
            rules.get("return_and_refund_scenarios", [])
        )

        # 1. 查 OrderService 拿订单状态 + 金额（防越权）
        order = await self._get_order_service().get_order(user_id=user_id, order_no=order_no)
        if order is None:
            from app.services.after_sales.protocols import OrderNotFoundForAdviceError
            raise OrderNotFoundForAdviceError(
                f"订单不存在或越权: user_id={user_id} order_no={order_no}"
            )

        order_status = order.status
        total_amount = order.total_amount

        # 2. 优先级匹配：先 refund_only 精确场景（any_status 兜底放最后）
        #    any_status 不是无脑匹配 —— 只在所有精确场景都不命中时才用
        fallback_refund_only = None
        for scenario in refund_only_scenarios:
            if scenario.get("any_status"):
                fallback_refund_only = scenario
                continue
            if scenario.get("order_status") == order_status:
                cond = scenario.get("condition", "")
                # 低价值场景：仅当订单金额确实 < 阈值
                if "低于 50 元" in cond and total_amount >= low_value_threshold:
                    continue
                return RefundTypeAdvice(
                    order_no=order_no,
                    recommended_type="refund_only",
                    reasoning=cond,
                    conditions=[cond],
                )

        # 3. 然后匹配 return_and_refund
        for scenario in return_and_refund_scenarios:
            if scenario.get("order_status") == order_status:
                cond = scenario.get("condition", "")
                # 高价值场景：仅当订单金额确实 >= 阈值
                if "高于 50 元" in cond and total_amount < low_value_threshold:
                    continue
                return RefundTypeAdvice(
                    order_no=order_no,
                    recommended_type="return_and_refund",
                    reasoning=cond,
                    conditions=[cond],
                )

        # 4. 兜底：refund_only 的 any_status 场景
        if fallback_refund_only is not None:
            cond = fallback_refund_only.get("condition", "")
            return RefundTypeAdvice(
                order_no=order_no,
                recommended_type="refund_only",
                reasoning=cond,
                conditions=[cond],
            )

        # 5. 最终默认：低价值 refund_only，否则 return_and_refund
        if total_amount < low_value_threshold:
            return RefundTypeAdvice(
                order_no=order_no,
                recommended_type="refund_only",
                reasoning=f"订单金额 ¥{total_amount:.0f} 较低（< {low_value_threshold:.0f}），建议仅退款",
                conditions=[f"订单金额 < ¥{low_value_threshold:.0f}"],
            )
        return RefundTypeAdvice(
            order_no=order_no,
            recommended_type="return_and_refund",
            reasoning=f"订单金额 ¥{total_amount:.0f}，建议退货退款",
            conditions=[f"订单金额 ≥ ¥{low_value_threshold:.0f}"],
        )