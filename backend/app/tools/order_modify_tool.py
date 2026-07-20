"""
OrderModifyTool - 售中订单修改 Tool（Sprint 19 · 写操作 2 步确认机制）

按 CLAUDE.md §9.3.3 + §9.5.1：写操作强制 user_id 防越权，走 Service Protocol 不直连 ORM。
按 CLAUDE.md §9.7 自检 5 问：依赖 OrderModifyService Protocol + Factory。

V2 关键设计（spec §2.3 + 用户拍板）：
- **写操作必须 confirmed=True** 才执行；否则返 needs_confirmation dict
- 确认 dict 格式固定：{"status": "needs_confirmation", "prompt": "...", "requires_user_input": True}
- LLM 看到 needs_confirmation → 主动询问用户 → 用户确认 → 同订单同操作再次调用 confirmed=True
- 这层防护在 Tool 内部；dispatch() 不需要改

3 个写方法：
- modify_address(user_id, order_no, new_address, confirmed=False)
- modify_item_spec(user_id, order_no, sku, new_qty, confirmed=False)
- merge_orders(user_id, order_nos, confirmed=False)
"""
from __future__ import annotations

import logging
from typing import List, Optional

from app.schemas.business import MergeResult, ModifyResult
from app.services.order.factory import run_sync
from app.services.order_modify.factory import get_order_modify_service_factory
from app.services.order_modify.protocols import OrderModifyService

logger = logging.getLogger(__name__)


# =============================================================
# 确认机制（统一 helper · spec §2.3）
# =============================================================
def _check_confirmation(confirmed: bool, action_desc: str) -> Optional[dict]:
    """写操作确认门禁（V2 简化版）。

    Args:
        confirmed: 用户是否已确认（True = 已确认；False = 未确认）
        action_desc: 操作中文描述，用于生成 prompt

    Returns:
        None: confirmed=True，可继续执行写操作
        dict: confirmed=False，返 needs_confirmation dict 让 LLM 询问用户

    设计：返回 dict 而非抛异常，因为 Tool 返 dict 是契约；dispatch 会把 dict 透传给 LLM。
    """
    if not confirmed:
        return {
            "status": "needs_confirmation",
            "prompt": f"即将{action_desc}，请用户回复「确认」后再执行。",
            "requires_user_input": True,
        }
    return None


class OrderModifyTool:
    """售中订单修改 Tool（写操作 · 2 步确认）

    所有写方法：
    1. 默认 confirmed=False → 触发确认门禁，返 needs_confirmation dict
    2. 传入 confirmed=True → 调 OrderModifyService Protocol 执行实际写操作
    """

    # =============================================================
    # 1. 修改收货地址
    # =============================================================
    @staticmethod
    def modify_address(
        user_id: int, order_no: str, new_address: str, confirmed: bool = False,
    ) -> dict:
        """修改收货地址（限未发货订单）。

        Args:
            user_id: 用户 ID（防越权）
            order_no: 订单号
            new_address: 新地址字符串
            confirmed: 用户是否已确认（默认 False 触发确认提示）

        Returns:
            confirmed=False: {"status": "needs_confirmation", "prompt": "...", "requires_user_input": True}
            confirmed=True: ModifyResult.model_dump() dict（含 success / reason / snapshots）
            异常路径: {"error": "..."} dict
        """
        # Step 1: 确认门禁
        action_desc = f"将订单 {order_no} 的收货地址改为「{new_address}」"
        confirmation = _check_confirmation(confirmed, action_desc)
        if confirmation is not None:
            return confirmation

        # Step 2: 调 Service 写
        try:
            svc: OrderModifyService = (
                get_order_modify_service_factory().get_order_modify_service()
            )
            result: ModifyResult = run_sync(
                svc.modify_address(user_id, order_no, new_address),
            )
            return result.model_dump()
        except Exception as e:
            logger.warning(
                f"OrderModifyTool.modify_address 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}

    # =============================================================
    # 2. 修改商品规格/数量
    # =============================================================
    @staticmethod
    def modify_item_spec(
        user_id: int, order_no: str, sku: str,
        new_qty: Optional[int] = None, confirmed: bool = False,
    ) -> dict:
        """修改商品规格/数量（限未发货订单；V2 仅支持数量调整）。

        Args:
            user_id: 用户 ID（防越权）
            order_no: 订单号
            sku: 目标 SKU
            new_qty: 新数量（None = 不调整数量；V2 暂不支持换 SKU）
            confirmed: 用户是否已确认

        Returns:
            confirmed=False: needs_confirmation dict
            confirmed=True: ModifyResult.model_dump() dict
            异常路径: {"error": "..."} dict
        """
        qty_desc = f"调整为 {new_qty} 件" if new_qty is not None else "修改规格"
        action_desc = f"将订单 {order_no} 的 SKU={sku} {qty_desc}"
        confirmation = _check_confirmation(confirmed, action_desc)
        if confirmation is not None:
            return confirmation

        try:
            svc: OrderModifyService = (
                get_order_modify_service_factory().get_order_modify_service()
            )
            result: ModifyResult = run_sync(
                svc.modify_item_spec(user_id, order_no, sku, new_qty),
            )
            return result.model_dump()
        except Exception as e:
            logger.warning(
                f"OrderModifyTool.modify_item_spec 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}

    # =============================================================
    # 3. 合并订单
    # =============================================================
    @staticmethod
    def merge_orders(
        user_id: int, order_nos: List[str], confirmed: bool = False,
    ) -> dict:
        """合并订单（限同一店铺 + 未发货 + 5 分钟内）。

        Args:
            user_id: 用户 ID（防越权）
            order_nos: 待合并订单号列表（≥ 2）
            confirmed: 用户是否已确认

        Returns:
            confirmed=False: needs_confirmation dict
            confirmed=True: MergeResult.model_dump() dict
            异常路径: {"error": "..."} dict
        """
        order_list_str = "、".join(order_nos)
        action_desc = f"合并订单 {order_list_str}"
        confirmation = _check_confirmation(confirmed, action_desc)
        if confirmation is not None:
            return confirmation

        try:
            svc: OrderModifyService = (
                get_order_modify_service_factory().get_order_modify_service()
            )
            result: MergeResult = run_sync(
                svc.merge_orders(user_id, list(order_nos)),
            )
            return result.model_dump()
        except Exception as e:
            logger.warning(
                f"OrderModifyTool.merge_orders 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}


# 暴露给测试用例（单测验证 confirmation dict 形态）
__all__ = ["OrderModifyTool", "_check_confirmation"]