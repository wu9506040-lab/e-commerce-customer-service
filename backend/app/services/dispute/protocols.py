"""
DisputeService Protocol（CLAUDE.md §9.3.2 支持模块替换）

V2 范围：纯规则查询，不调 LLM，不实际举报/介入。
业务规则从 config/business_rules/dispute.yaml 加载（§9.4.2）。
默认实现：YamlDisputeService（静态规则 + OrderService 查询订单金额判定举证责任）。

3 个场景：质量问题鉴定 / 平台介入资格 / 举报假货流程。

设计（CLAUDE.md §9.7 自检 5 问）：
- Q1 业务模块依赖此 Protocol，不依赖具体实现
- Q3 Protocol 先于实现（本文件先定义签名）
- 方法全 async（spec §8 决策 #4；FastAPI 友好）

接入方：自建业务中台 / 客服系统实现此接口即可对接 AI 客服售后纠纷咨询。
"""
from typing import Protocol, runtime_checkable

from app.schemas.business import (
    DisputeError,
    PlatformInterveneCheck,
    QualityDisputeProcess,
    ReportFakeGoodsProcess,
)


@runtime_checkable
class DisputeService(Protocol):
    """售后纠纷处理协议"""

    async def get_quality_dispute_process(
        self, user_id: int, order_no: str,
    ) -> QualityDisputeProcess:
        """质量问题鉴定流程

        返回：举证责任方 / 举证时限 / 处理步骤 / 上诉渠道。
        举证责任：按订单金额判定（> high_value_threshold → seller；否则 buyer）。
        """
        ...

    async def check_platform_intervene_eligibility(
        self, user_id: int, order_no: str, dispute_type: str,
    ) -> PlatformInterveneCheck:
        """平台介入条件检查

        dispute_type: "refund_rejected"/"no_response"/"partial_refund"
        检查：是否已与卖家沟通 N 次 / 是否有聊天记录 / 时限内。
        V2 简化：仅返 YAML 规则 + 类型要求，不查聊天记录实际轮次。
        """
        ...

    async def get_report_fake_goods_process(
        self, order_no: str,
    ) -> ReportFakeGoodsProcess:
        """举报假货流程（V2 仅规则说明，不实际举报）

        返回：举报路径 / 需要证据 / 处罚措施 / 处理时限。
        """
        ...


@runtime_checkable
class DisputeServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_dispute_service(self) -> DisputeService: ...


# === 异常类（就近导出，避免污染业务模块） ===
# 注：DisputeError 已定义在 app.schemas.business（统一管理业务异常基类）
