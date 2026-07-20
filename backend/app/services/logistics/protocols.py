"""
LogisticsService Protocol（CLAUDE.md §9.3.2 支持模块替换）

V2 范围：仅查询（query / track / get_carriers），不支持下单/取消（V3+ 留）。
当前默认实现：Mock（基于订单状态生成 mock 轨迹，无真实物流表）。
接入方：自建商城/ERP/快递100 实现此接口即可对接 AI 客服的物流查询。

设计（CLAUDE.md §9.7 自检 5 问）：
- Q1 业务模块（Tool 层）依赖此 Protocol，不依赖具体实现
- Q3 Protocol 先于实现（本文件先定义签名）
- 方法全 async（spec §8 决策 #4；FastAPI 友好）
"""
from typing import List, Protocol, runtime_checkable

from app.schemas.business import Logistics, TrackingInfo


@runtime_checkable
class LogisticsService(Protocol):
    """物流服务协议"""

    async def query(self, order_no: str) -> Logistics | None:
        """按订单号查物流汇总（carrier + 状态 + 最新位置）；订单不存在返 None"""
        ...

    async def track(self, tracking_no: str) -> TrackingInfo | None:
        """按运单号查完整轨迹（events 列表）；运单号无效返 None"""
        ...

    async def get_carriers(self) -> List[str]:
        """支持的快递公司列表（V2 mock 返 ['顺丰', '中通', '圆通', '韵达']）"""
        ...


@runtime_checkable
class LogisticsServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_logistics_service(self) -> LogisticsService: ...


# === 异常类 ===
class LogisticsError(Exception): ...
class TrackingNotFoundError(LogisticsError): ...