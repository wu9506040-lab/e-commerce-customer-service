"""
InvoiceService Protocol（CLAUDE.md §9.3.2 支持模块替换）

V2 范围：仅发票资格检查，不实际开发票。
业务规则从 config/business_rules/invoice.yaml 加载（§9.4.2）。
默认实现：YamlInvoiceService（静态规则 + OrderService 查询订单金额判断满额条件）。

1 个场景：发票申请资格检查（满额 + 普通 vs 电子 vs 公司专票）。

设计（CLAUDE.md §9.7 自检 5 问）：
- Q1 业务模块依赖此 Protocol，不依赖具体实现
- Q3 Protocol 先于实现（本文件先定义签名）
- 方法全 async（spec §8 决策 #4；FastAPI 友好）

接入方：自建业务中台 / 客服系统实现此接口即可对接 AI 客服发票咨询。
实际开票 = 接入企业 ERP / 税控系统（V3+ 留）。
"""
from typing import Protocol, runtime_checkable

from app.schemas.business import (
    InvoiceEligibility,
)


@runtime_checkable
class InvoiceService(Protocol):
    """发票申请服务协议"""

    async def check_invoice_eligibility(
        self, user_id: int, order_no: str, invoice_type: str,
    ) -> InvoiceEligibility:
        """发票申请资格检查

        invoice_type: "personal_paper" / "personal_electronic" / "company_special"
        检查：满额条件 / 订单状态 / 企业资质（公司专票）。
        V2 仅资格检查，不实际开具发票。
        """
        ...


@runtime_checkable
class InvoiceServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_invoice_service(self) -> InvoiceService: ...
