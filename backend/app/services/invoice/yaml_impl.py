"""
InvoiceService YAML 默认实现 — Sprint 20 通用客服中台（spec §4）

V2 范围：仅发票资格检查 + 流程说明，不实际开具发票。
业务规则来源：YAML.INVOICE_RULES
订单金额查询：通过 OrderService Protocol。
"""
from typing import Any, Dict, List, Optional

from app.schemas.business import (
    InvoiceEligibility,
)
from app.services.config_loader import get_config_loader
from app.services.order.factory import get_order_service_factory
from app.services.order.protocols import OrderService


# 申请 URL 前缀（V2 简化：纯占位，实际接企业 ERP 时替换）
APPLICATION_URL_BASE = "https://invoice.example.com/apply"


class YamlInvoiceService:
    """InvoiceService Protocol 的 YAML 默认实现

    - 静态规则：从 invoice.yaml 启动期加载
    - 动态数据：通过 OrderService Protocol 查订单金额（满额条件判定）
    """

    def __init__(
        self,
        order_service: Optional[OrderService] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._order_service = order_service
        self._config = config if config is not None else get_config_loader().load("invoice")

    def _get_order_service(self) -> OrderService:
        if self._order_service is None:
            self._order_service = get_order_service_factory().get_order_service()
        return self._order_service

    # =============================================================
    # 发票申请资格检查
    # =============================================================
    async def check_invoice_eligibility(
        self, user_id: int, order_no: str, invoice_type: str,
    ) -> InvoiceEligibility:
        rules: Dict[str, Any] = self._config.get("INVOICE_RULES", {})
        minimum_amount: float = float(rules.get("minimum_amount", 50.0))
        type_requirements: Dict[str, Any] = dict(rules.get("invoice_type_requirements", {}))
        notes_base: List[str] = list(rules.get("notes", []))

        # 1. 校验 invoice_type
        if invoice_type not in type_requirements:
            return InvoiceEligibility(
                eligible=False,
                order_no=order_no,
                invoice_type=invoice_type,
                invoice_title_required=False,
                tax_id_required=False,
                amount_threshold_met=False,
                minimum_amount=minimum_amount,
                current_order_amount=0.0,
                eligible_amount=0.0,
                application_url=APPLICATION_URL_BASE,
                notes=[
                    f"不支持的发票类型: {invoice_type}，可选: {list(type_requirements.keys())}",
                ],
            )

        type_cfg: Dict[str, Any] = dict(type_requirements.get(invoice_type, {}))
        title_required: bool = bool(type_cfg.get("title_required", False))
        tax_id_required: bool = bool(type_cfg.get("tax_id_required", False))
        shipping_fee_borne_by: str = type_cfg.get("shipping_fee_borne_by", "buyer")
        processing_days: int = int(type_cfg.get("processing_days", 7))
        additional_requirements: List[str] = list(type_cfg.get("additional_requirements", []))

        # 2. 查订单金额（防越权）
        order = await self._get_order_service().get_order(user_id=user_id, order_no=order_no)
        if order is None:
            return InvoiceEligibility(
                eligible=False,
                order_no=order_no,
                invoice_type=invoice_type,
                invoice_title_required=title_required,
                tax_id_required=tax_id_required,
                amount_threshold_met=False,
                minimum_amount=minimum_amount,
                current_order_amount=0.0,
                eligible_amount=0.0,
                application_url=APPLICATION_URL_BASE,
                notes=[f"订单不存在或越权访问: order_no={order_no}"] + notes_base,
            )

        current_amount = order.total_amount
        amount_threshold_met = current_amount >= minimum_amount
        eligible_amount = current_amount if amount_threshold_met else 0.0
        eligible = amount_threshold_met

        # 3. 拼接 notes
        notes: List[str] = list(notes_base)
        if not amount_threshold_met:
            notes.append(f"订单金额 ¥{current_amount:.2f} 不足 ¥{minimum_amount:.0f} 最低开票额")
        else:
            notes.append(f"订单金额 ¥{current_amount:.2f} ≥ ¥{minimum_amount:.0f}，满足满额条件")
        notes.append(f"处理时限: {processing_days} 个工作日")
        if shipping_fee_borne_by == "buyer":
            notes.append("纸质发票邮费由买家承担")
        elif shipping_fee_borne_by == "none":
            notes.append("电子发票无邮费")
        if additional_requirements:
            notes.append("公司专票附加要求: " + "、".join(additional_requirements))

        # 4. 申请 URL 拼接
        application_url = f"{APPLICATION_URL_BASE}?order_no={order_no}&type={invoice_type}"

        return InvoiceEligibility(
            eligible=eligible,
            order_no=order_no,
            invoice_type=invoice_type,
            invoice_title_required=title_required,
            tax_id_required=tax_id_required,
            amount_threshold_met=amount_threshold_met,
            minimum_amount=minimum_amount,
            current_order_amount=current_amount,
            eligible_amount=eligible_amount,
            application_url=application_url,
            notes=notes,
        )
