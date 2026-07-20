"""
OrderModifyService Protocol 契约（Sprint 18-C）

CLAUDE.md §9.3.2 支持模块替换：
- 接入方（自营订单系统）实现该接口对接自家修改流程
- 默认 MySQL 实现见 mysql_impl.py
- V2 范围：3 种修改操作；超出范围返 ModifyNotAllowedError 或对应业务异常
- 防越权（§9.5.1）：所有写方法强制 user_id 验证，通过 OrderService.get_order 内部 user_id 过滤

接口就近原则（§7.3）：本文件是 order_modify 模块唯一对外契约入口。
"""
from typing import List, Optional, Protocol, runtime_checkable

from app.schemas.business import (
    MergeResult,
    ModifyError,
    ModifyNotAllowedError,
    ModifyResult,
    OrderNotFoundError,
    MergeConditionError,
)


# 允许设置的修改状态机（硬编码 · 业务强约束；V2 暂不放 YAML，§9.4.2 扩展点已标注在方法 docstring）
MODIFIABLE_STATUSES = frozenset({"pending", "paid"})
NON_MODIFIABLE_REASON_PREFIX = "当前订单状态不可修改"

# 合并订单时间窗
MERGE_TIME_WINDOW_MINUTES = 5


@runtime_checkable
class OrderModifyService(Protocol):
    """售中订单修改服务协议

    所有写方法强制 user_id 防越权（CLAUDE.md §9.5.1）。
    """

    async def modify_address(
        self, user_id: int, order_no: str, new_address: str,
    ) -> ModifyResult:
        """修改收货地址（限未发货订单）。

        Args:
            user_id: 调用方用户 ID（必传；与订单所属用户不匹配抛 OrderNotFoundError）
            order_no: 订单号
            new_address: 新地址字符串

        Returns:
            ModifyResult(success=True/False, modification_type="address", reason, ...)

        Raises:
            OrderNotFoundError: 订单不存在或 user_id 不匹配
            ModifyNotAllowedError: 订单状态非 pending/paid

        扩展点（CLAUDE.md §9.4.2）：MODIFIABLE_STATUSES 后续可下沉到
        config/business_rules/order_modify.yaml；V2 硬编码简化。
        """
        ...

    async def modify_item_spec(
        self, user_id: int, order_no: str, sku: str, new_qty: Optional[int] = None,
    ) -> ModifyResult:
        """修改商品规格/数量（限未发货订单）。

        V2 仅支持数量调整；换 SKU 留 V3+。
        数量 new_qty=None 表示不调整数量。

        Args:
            user_id: 调用方用户 ID（防越权）
            order_no: 订单号
            sku: 目标 SKU
            new_qty: 新数量（None=不调整）

        Returns:
            ModifyResult(success=True/False, modification_type="spec", reason, ...)

        Raises:
            OrderNotFoundError: 订单不存在或越权
            ModifyNotAllowedError: 订单状态不允许修改
        """
        ...

    async def merge_orders(
        self, user_id: int, order_nos: List[str],
    ) -> MergeResult:
        """合并订单（限同一店铺 + 未发货 + 5 分钟内）。

        条件校验（任一不满足抛 MergeConditionError）：
        1. 所有订单同 user_id + 同店铺（OrderItem.product_id 范畴等价）
        2. 所有订单状态均为 pending/paid
        3. 最早订单距今不超过 MERGE_TIME_WINDOW_MINUTES

        Args:
            user_id: 调用方用户 ID（防越权）
            order_nos: 待合并订单号列表（≥ 2）

        Returns:
            MergeResult(success=True/False, primary_order_no, merged_order_nos, reason)

        Raises:
            OrderNotFoundError: 订单不存在或越权
            MergeConditionError: 合并条件不满足
        """
        ...


# 暴露异常类 + 状态常量（外部模块无需 import 修改 Schema 文件）
__all__ = [
    "OrderModifyService",
    "MODIFIABLE_STATUSES",
    "MERGE_TIME_WINDOW_MINUTES",
    "ModifyError",
    "ModifyNotAllowedError",
    "OrderNotFoundError",
    "MergeConditionError",
]
