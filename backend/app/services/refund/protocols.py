"""
RefundService Protocol（CLAUDE.md §9.3.2 支持模块替换）

V2 范围：仅做读（list / get / get_status），写操作（create / update）留 V3+。
—— YAGNI：当前业务场景不需要 AI 自动创建/审批退款（需人工介入审核）。
接入方：自建商城/OA/ERP 实现此接口即可对接 AI 客服的退款查询。

设计（CLAUDE.md §9.7 自检 5 问）：
- Q1 业务模块（Tool 层）依赖此 Protocol，不依赖具体实现
- Q3 Protocol 先于实现（本文件先定义签名）
- 方法全 async（spec §8 决策 #4；FastAPI 友好）
"""
from datetime import datetime
from typing import List, Optional, Protocol, runtime_checkable

from app.schemas.business import Refund


@runtime_checkable
class RefundService(Protocol):
    """退款服务协议"""

    async def get_refund(self, user_id: int, refund_no: str) -> Optional[Refund]:
        """按退款号查退款详情（含 order_no 关联，强制 user_id 防越权）；不存在返 None"""
        ...

    async def list_user_refunds(
        self, user_id: int, status: Optional[str] = None,
        start_date: Optional[datetime] = None, end_date: Optional[datetime] = None,
        limit: int = 20, cursor: Optional[str] = None,
    ) -> tuple[List[Refund], Optional[str]]:
        """查用户退款列表；返 (refunds, next_cursor)；cursor 为下一页游标（无更多则 None）"""
        ...

    async def get_refund_status(self, refund_no: str) -> Optional[str]:
        """查退款当前状态（pending / approved / rejected / completed）；不存在返 None"""
        ...


@runtime_checkable
class RefundServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_refund_service(self) -> RefundService: ...


# === 异常类 ===
class RefundError(Exception): ...
class RefundNotFoundError(RefundError): ...