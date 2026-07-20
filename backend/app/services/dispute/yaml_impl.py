"""
DisputeService YAML 默认实现 — Sprint 20 通用客服中台（spec §3）

V2 简化：售后纠纷规则是静态业务规则 + 订单金额查询（举证责任判定）。
业务规则来源：
- 质量问题鉴定：YAML.QUALITY_DISPUTE_RULES
- 平台介入：YAML.PLATFORM_INTERVENE_RULES
- 举报假货：YAML.REPORT_FAKE_GOODS_RULES

订单金额查询：通过 OrderService Protocol（mock 友好、越权防护内建）。
"""
from typing import Any, Dict, List, Optional

from app.schemas.business import (
    PlatformInterveneCheck,
    QualityDisputeProcess,
    ReportFakeGoodsProcess,
)
from app.services.config_loader import get_config_loader
from app.services.order.factory import get_order_service_factory
from app.services.order.protocols import OrderService


class YamlDisputeService:
    """DisputeService Protocol 的 YAML 默认实现

    - 静态规则：从 dispute.yaml 启动期加载
    - 动态数据：通过 OrderService Protocol 查订单金额（举证责任判定）
    """

    def __init__(
        self,
        order_service: Optional[OrderService] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._order_service = order_service
        self._config = config if config is not None else get_config_loader().load("dispute")

    def _get_order_service(self) -> OrderService:
        if self._order_service is None:
            self._order_service = get_order_service_factory().get_order_service()
        return self._order_service

    # =============================================================
    # 1. 质量问题鉴定流程
    # =============================================================
    async def get_quality_dispute_process(
        self, user_id: int, order_no: str,
    ) -> QualityDisputeProcess:
        rules: Dict[str, Any] = self._config.get("QUALITY_DISPUTE_RULES", {})
        burden_cfg: Dict[str, Any] = rules.get("burden_of_proof_by_value", {})
        high_threshold: float = float(burden_cfg.get("high_value_threshold", 1000.0))
        low_burden: str = burden_cfg.get("low_value_burden", "buyer")
        evidence_deadline_hours: int = int(rules.get("evidence_deadline_hours", 48))
        process_steps: List[str] = list(rules.get("process_steps", []))
        appeal_channels: List[str] = list(rules.get("appeal_channels", []))

        # 默认举证责任（订单金额未知时按低价值处理）
        burden = low_burden
        evidence_required: List[str] = []

        # 1. 查订单金额（防越权）
        order = await self._get_order_service().get_order(user_id=user_id, order_no=order_no)
        if order is not None:
            total = order.total_amount
            if total > high_threshold:
                burden = "seller"     # 高价值 → 卖家举证
            else:
                burden = low_burden    # 低价值 → 按配置默认（buyer）

        # 2. 按举证方决定所需证据
        if burden == "seller":
            evidence_required = [
                "商品发货时质检报告",
                "商品出库照片 / 视频",
                "权威机构鉴定（如有）",
            ]
        else:
            evidence_required = [
                "商品问题照片 / 视频",
                "与卖家沟通记录",
            ]

        return QualityDisputeProcess(
            order_no=order_no,
            burden_of_proof=burden,
            evidence_required=evidence_required,
            evidence_deadline_hours=evidence_deadline_hours,
            process_steps=process_steps,
            appeal_channels=appeal_channels,
        )

    # =============================================================
    # 2. 平台介入条件检查
    # =============================================================
    async def check_platform_intervene_eligibility(
        self, user_id: int, order_no: str, dispute_type: str,
    ) -> PlatformInterveneCheck:
        rules: Dict[str, Any] = self._config.get("PLATFORM_INTERVENE_RULES", {})
        min_rounds: int = int(rules.get("min_communication_rounds", 3))
        must_have_log: bool = bool(rules.get("must_have_chat_log", True))
        deadline_hours: int = int(rules.get("deadline_hours", 168))
        type_requirements: Dict[str, List[str]] = dict(rules.get("dispute_type_requirements", {}))
        consequences_cfg: Dict[str, List[str]] = dict(rules.get("consequences", {}))

        # 1. 校验 dispute_type
        if dispute_type not in type_requirements:
            return PlatformInterveneCheck(
                eligible=False,
                order_no=order_no,
                dispute_type=dispute_type,
                reason=f"不支持的纠纷类型: {dispute_type}，可选: {list(type_requirements.keys())}",
                required_conditions=[],
                consequences=[],
            )

        # 2. V2 简化：仅列规则说明，不查聊天记录实际轮次
        required = list(type_requirements.get(dispute_type, []))
        if must_have_log:
            required.append(f"至少与卖家沟通 {min_rounds} 轮 + 有聊天记录")
        else:
            required.append(f"至少与卖家沟通 {min_rounds} 轮")

        required.append(f"订单完成后 {deadline_hours} 小时内（即 7 天内）")

        # 3. 后果（合并买卖双方）
        all_consequences: List[str] = []
        for side in ("buyer_side", "seller_side"):
            for item in consequences_cfg.get(side, []):
                all_consequences.append(item)

        # 4. V2 简化：因不查聊天记录，按"满足条件"返 True（让用户自查）
        return PlatformInterveneCheck(
            eligible=True,
            order_no=order_no,
            dispute_type=dispute_type,
            reason=f"满足介入条件可向平台申请（deadline={deadline_hours}小时）",
            required_conditions=required,
            consequences=all_consequences,
        )

    # =============================================================
    # 3. 举报假货流程
    # =============================================================
    async def get_report_fake_goods_process(
        self, order_no: str,
    ) -> ReportFakeGoodsProcess:
        rules: Dict[str, Any] = self._config.get("REPORT_FAKE_GOODS_RULES", {})
        report_channels: List[str] = list(rules.get("report_channels", []))
        evidence_required: List[str] = list(rules.get("evidence_required", []))
        possible_penalties: List[str] = list(rules.get("possible_penalties", []))
        processing_days: int = int(rules.get("processing_days", 7))

        notes = [
            "举报前请先与卖家沟通并保留证据",
            "提交后平台将在处理时限内反馈结果",
            "情况严重可同步拨打 12315 消费者热线",
        ]

        return ReportFakeGoodsProcess(
            order_no=order_no,
            report_channels=report_channels,
            evidence_required=evidence_required,
            possible_penalties=possible_penalties,
            processing_days=processing_days,
            notes=notes,
        )
