"""
PromotionTool - 售前优惠规则咨询 Tool（Sprint 19）

按 CLAUDE.md §9.3.3：业务模块依赖 Protocol（PromotionRuleService），不直连 YamlPromotionRuleService。
按 CLAUDE.md §9.7 自检 5 问：Tool 类不依赖具体 Service 实现，依赖 Protocol + Factory。

V2 范围（spec §2.2）：
- 3 个静态方法：get_active_promotions / check_coupon_stackable / calculate_bundle_discount
- 只读操作，无 confirmed 参数
- Promotion 返回 List[Promotion] → [dict, dict, ...] 序列化为 list
"""
from __future__ import annotations

import logging
from typing import List, Optional

from app.schemas.business import (
    BundleDiscountResult,
    CouponStackResult,
    Promotion,
)
from app.services.order.factory import run_sync
from app.services.promotion.factory import get_promotion_rule_service_factory
from app.services.promotion.protocols import PromotionRuleService

logger = logging.getLogger(__name__)


def _promotions_to_list(promotions: Optional[List[Promotion]]) -> dict:
    """List[Promotion] → {"promotions": [dict, ...]} dict 格式。

    设计：与现有 _run_search_product 的 {"products": [...]} 风格保持一致，
    便于 LLM 消费时统一「列表结果」心智模型。
    """
    if promotions is None:
        return {"promotions": []}
    return {"promotions": [p.model_dump() for p in promotions]}


class PromotionTool:
    """售前优惠规则咨询 Tool（只读；V2 仅 Service 接口适配）"""

    # =============================================================
    # 1. 当前可用优惠活动
    # =============================================================
    @staticmethod
    def get_active_promotions(user_id: int, cart_items: Optional[list] = None) -> dict:
        """当前用户可用的优惠活动（按时间窗 + 适用店铺/类目过滤）。

        Args:
            user_id: 用户 ID（V2 不差异化；预留灰度）
            cart_items: 购物车商品列表（V2 可选；用于类目/店铺匹配）
                       [{"sku", "qty", "unit_price", "store_id", "category"}, ...]

        Returns:
            {"promotions": [Promotion.model_dump() dict, ...]}
            或 {"error": "..."} dict
        """
        try:
            svc: PromotionRuleService = (
                get_promotion_rule_service_factory().get_promotion_rule_service()
            )
            promos: List[Promotion] = run_sync(
                svc.get_active_promotions(user_id, list(cart_items or [])),
            )
            return _promotions_to_list(promos)
        except Exception as e:
            logger.warning(
                f"PromotionTool.get_active_promotions 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}

    # =============================================================
    # 2. 优惠券叠加校验
    # =============================================================
    @staticmethod
    def check_coupon_stackable(coupon_ids: List[str]) -> dict:
        """多张优惠券叠加校验：哪些可叠加 + 哪些互斥 + 最佳组合。

        Args:
            coupon_ids: 优惠券 ID 列表

        Returns:
            CouponStackResult.model_dump() dict（含 stackable_groups /
                                             conflicting_pairs / best_combination）
            或 {"error": "..."} dict
        """
        try:
            svc: PromotionRuleService = (
                get_promotion_rule_service_factory().get_promotion_rule_service()
            )
            result: Optional[CouponStackResult] = run_sync(
                svc.check_coupon_stackable(list(coupon_ids)),
            )
            if result is None:
                return {"error": "coupon_stackable: no result"}
            return result.model_dump()
        except Exception as e:
            logger.warning(
                f"PromotionTool.check_coupon_stackable 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}

    # =============================================================
    # 3. 跨店满减计算
    # =============================================================
    @staticmethod
    def calculate_bundle_discount(store_totals: dict) -> dict:
        """跨店满减计算：当前合计 + 距离下一档 + 凑单建议。

        Args:
            store_totals: 每家店金额 {"storeA": 280, "storeB": 150, ...}

        Returns:
            BundleDiscountResult.model_dump() dict
            或 {"error": "..."} dict
        """
        try:
            svc: PromotionRuleService = (
                get_promotion_rule_service_factory().get_promotion_rule_service()
            )
            result: Optional[BundleDiscountResult] = run_sync(
                svc.calculate_bundle_discount(dict(store_totals)),
            )
            if result is None:
                return {"error": "bundle_discount: no result"}
            return result.model_dump()
        except Exception as e:
            logger.warning(
                f"PromotionTool.calculate_bundle_discount 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}


__all__ = ["PromotionTool"]