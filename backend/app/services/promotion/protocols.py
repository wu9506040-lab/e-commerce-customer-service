"""
PromotionRuleService Protocol（CLAUDE.md §9.3.2 支持模块替换 · Sprint 18 B）

售前优惠规则咨询（促销活动 / 优惠券叠加 / 跨店满减）。
V2 范围：纯规则解释，不下单，不调真实优惠计算服务（V3+ 接营销中台）。
业务规则从 config/business_rules/promotion.yaml 加载（§9.4.2）。

设计（CLAUDE.md §9.7 自检 5 问）：
- Q1 业务模块（未来 Tool 层）依赖此 Protocol，不依赖具体实现
- Q3 Protocol 先于实现（本文件先定义签名）
- 方法全 async（spec §8 决策 #4；FastAPI 友好）
- 不依赖 OrderService / RefundService（售前规则独立，§5 Scope Lock）
"""
from typing import Dict, List, Protocol, runtime_checkable

from app.schemas.business import BundleDiscountResult, CouponStackResult, Promotion


@runtime_checkable
class PromotionRuleService(Protocol):
    """售前优惠规则协议"""

    async def get_active_promotions(
        self, user_id: int, cart_items: List[dict],
    ) -> List[Promotion]:
        """当前用户可用优惠活动（按时间窗 + 适用店铺/类目过滤）

        Args:
            user_id: 用户 ID（V2 不差异化；预留灰度/用户分层）
            cart_items: [{"sku": "X", "qty": 1, "unit_price": 100, "store_id": "storeA", "category": "美妆"}, ...]

        Returns:
            命中规则的 Promotion 列表（仅在时间窗内 + 店铺/类目匹配）
        """
        ...

    async def check_coupon_stackable(
        self, coupon_ids: List[str],
    ) -> CouponStackResult:
        """多张优惠券叠加校验

        业务规则：
        - 同类型券（如 2 张满减券）不可叠加（coupon_rules.same_type_mutual_exclusive）
        - 单订单最多叠加 max_stack_per_order 张
        - 按 conflict_rules 矩阵生成 conflicting_pairs

        Returns:
            CouponStackResult（stackable_groups / conflicting_pairs / best_combination）
        """
        ...

    async def calculate_bundle_discount(
        self, store_totals: Dict[str, float],
    ) -> BundleDiscountResult:
        """跨店满减计算（按 bundle_rules.tiers 找下一档）

        Args:
            store_totals: {"storeA": 280, "storeB": 150, "storeC": 90}

        Returns:
            BundleDiscountResult（current_total + 距离下一档 + 凑单建议）
            next_threshold=None 表示已达标最高档
        """
        ...


@runtime_checkable
class PromotionRuleServiceFactory(Protocol):
    """工厂协议 — FastAPI Depends 注入"""
    def get_promotion_rule_service(self) -> PromotionRuleService: ...


# === 异常类（CLAUDE.md §9.3.1 五件套之「异常处理」）===
class PromotionError(Exception):
    """售前规则基类异常"""


class PromotionConfigError(PromotionError):
    """promotion.yaml 字段缺失或格式错误（启动期 fail-fast；运行时按规则 fallback）"""