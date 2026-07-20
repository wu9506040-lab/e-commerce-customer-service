"""
YAML 默认实现 — YamlPromotionRuleService（Sprint 18 B）

实现 services/promotion/protocols.py 的 PromotionRuleService Protocol。
业务规则从 config/business_rules/promotion.yaml 启动期一次加载（config_loader 缓存）。
按时间窗 + 适用店铺/类目过滤 promotions；按 conflict_rules 判定券冲突；按 tiers 计算下一档满减。

设计要点：
- 不查数据库（spec §3 明确纯 YAML 规则匹配）
- 不依赖 OrderService / RefundService（售前规则独立，§5 Scope Lock）
- 不调用 LLM / 不下单（spec §3.1 V2 范围）
- 缺字段 fallback 到默认值（config_loader 已校验顶层 dict，字段级用 .get + 默认）
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from app.schemas.business import (
    BundleDiscountResult,
    CouponStackResult,
    Promotion,
)
from app.services.config_loader import get_config_loader
from app.services.promotion.protocols import (
    PromotionConfigError,
)

logger = logging.getLogger(__name__)


# =============================================================
# YAML 字段读取（启动期一次性）
# =============================================================
class _YamlSnapshot:
    """promotion.yaml 缓存快照（构造时一次 load；不改）"""

    def __init__(self) -> None:
        data = get_config_loader().load("promotion")
        # 启动期字段校验：缺关键字段直接抛（fail-fast）
        if "promotions" not in data or not isinstance(data["promotions"], list):
            raise PromotionConfigError("promotion.yaml 缺少 promotions 列表")
        if "coupon_rules" not in data or not isinstance(data["coupon_rules"], dict):
            raise PromotionConfigError("promotion.yaml 缺少 coupon_rules")
        if "bundle_rules" not in data or not isinstance(data["bundle_rules"], dict):
            raise PromotionConfigError("promotion.yaml 缺少 bundle_rules")

        self.promotions_raw: List[dict] = data["promotions"]
        self.coupon_rules_raw: dict = data["coupon_rules"]
        self.bundle_rules_raw: dict = data["bundle_rules"]


def _parse_dt(s: str) -> datetime:
    """ISO 时间字符串解析（promotion.yaml 时间格式）"""
    return datetime.fromisoformat(s)


def _to_promotion(p: dict) -> Promotion:
    """promotion.yaml 单条 → Promotion DTO"""
    return Promotion(
        promotion_id=p["promotion_id"],
        name=p["name"],
        type=p["type"],
        threshold=p.get("threshold"),
        benefit=p.get("benefit"),
        applicable_stores=list(p.get("applicable_stores") or []),
        applicable_categories=list(p.get("applicable_categories") or []),
        start_time=_parse_dt(p["start_time"]),
        end_time=_parse_dt(p["end_time"]),
        stackable=bool(p.get("stackable", False)),
    )


def _match_promotion(
    p: Promotion, cart_items: List[dict], now: datetime,
) -> bool:
    """单条 promotion 是否命中当前购物车

    匹配规则：
    1. 时间窗内（now ∈ [start_time, end_time]）
    2. 适用店铺：applicable_stores 为空 OR 任一 cart item 命中
    3. 适用类目：applicable_categories 为空 OR 任一 cart item 命中
    """
    # 1. 时间窗
    if not (p.start_time <= now <= p.end_time):
        return False

    # 空购物车 = 默认命中（业务方只问"有什么活动"）
    if not cart_items:
        return True

    # 2. 店铺过滤
    if p.applicable_stores:
        cart_stores = {item.get("store_id") for item in cart_items if item.get("store_id")}
        if not (cart_stores & set(p.applicable_stores)):
            return False

    # 3. 类目过滤
    if p.applicable_categories:
        cart_cats = {item.get("category") for item in cart_items if item.get("category")}
        if not (cart_cats & set(p.applicable_categories)):
            return False

    return True


# =============================================================
# YamlPromotionRuleService
# =============================================================
class YamlPromotionRuleService:
    """PromotionRuleService Protocol 的 YAML 默认实现

    设计：snapshot 在 __init__ 构造时一次性加载（config_loader 自身有缓存 + 启动期加载语义），
    业务调用零 I/O，纯内存规则匹配（满足 spec §3.3 不查数据库）。
    """

    def __init__(self) -> None:
        self._snap = _YamlSnapshot()
        # 预解析 promotions DTO（一次解析多次复用）
        self._promotions: List[Promotion] = [_to_promotion(p) for p in self._snap.promotions_raw]
        # 券定义索引：coupon_id → {"type": ..., "name": ...}
        self._coupon_index: Dict[str, dict] = {
            c["coupon_id"]: c for c in self._snap.coupon_rules_raw.get("coupon_definitions") or []
        }
        # 冲突规则索引
        self._conflicts: List[dict] = list(self._snap.coupon_rules_raw.get("conflict_rules") or [])
        self._same_type_mutual_exclusive: bool = bool(
            self._snap.coupon_rules_raw.get("same_type_mutual_exclusive", True)
        )
        self._max_stack: int = int(self._snap.coupon_rules_raw.get("max_stack_per_order", 3))
        # 满减档位（按 threshold 升序）
        raw_tiers = list(self._snap.bundle_rules_raw.get("tiers") or [])
        self._tiers: List[dict] = sorted(
            raw_tiers, key=lambda t: float(t["threshold"]),
        )

    # -------------------------------------------------
    # get_active_promotions
    # -------------------------------------------------
    async def get_active_promotions(
        self, user_id: int, cart_items: List[dict],
    ) -> List[Promotion]:
        now = datetime.now()
        result: List[Promotion] = []
        for p in self._promotions:
            if _match_promotion(p, cart_items, now):
                result.append(p)
        return result

    # -------------------------------------------------
    # check_coupon_stackable
    # -------------------------------------------------
    async def check_coupon_stackable(
        self, coupon_ids: List[str],
    ) -> CouponStackResult:
        # 0. 未知券过滤（未在 coupon_definitions 注册的视为 gift / 默认不冲突）
        resolved: List[tuple[str, str]] = []  # (coupon_id, type)
        unknown: List[str] = []
        for cid in coupon_ids:
            meta = self._coupon_index.get(cid)
            if meta is None:
                unknown.append(cid)
                resolved.append((cid, "gift"))  # 未知券降级为 gift（互不冲突）
            else:
                resolved.append((cid, str(meta.get("type", "gift"))))

        # 1. 计算 conflicting_pairs（双向扫描 conflict_rules）
        conflicts: List[dict] = []
        for i in range(len(resolved)):
            for j in range(i + 1, len(resolved)):
                cid_a, type_a = resolved[i]
                cid_b, type_b = resolved[j]
                for rule in self._conflicts:
                    if (rule.get("a_type") == type_a and rule.get("b_type") == type_b) or \
                       (rule.get("a_type") == type_b and rule.get("b_type") == type_a):
                        conflicts.append({
                            "a": cid_a,
                            "b": cid_b,
                            "reason": rule.get("reason", "互斥"),
                        })
                        break

        # 2. 构造 stackable_groups（按 type 分组；同组互斥 → 不同 type 可叠加）
        groups_map: Dict[str, List[str]] = {}
        for cid, ctype in resolved:
            groups_map.setdefault(ctype, []).append(cid)
        stackable_groups: List[List[str]] = [g for g in groups_map.values()]

        # 3. best_combination：从每组各取 1 张（避免互斥）+ 截断到 max_stack
        best: List[str] = []
        for g in stackable_groups:
            if len(best) >= self._max_stack:
                break
            best.append(g[0])
        if len(best) > self._max_stack:
            best = best[: self._max_stack]

        return CouponStackResult(
            stackable_groups=stackable_groups,
            conflicting_pairs=conflicts,
            best_combination=best,
        )

    # -------------------------------------------------
    # calculate_bundle_discount
    # -------------------------------------------------
    async def calculate_bundle_discount(
        self, store_totals: Dict[str, float],
    ) -> BundleDiscountResult:
        current_total = float(sum(store_totals.values()))

        # 找下一档：threshold > current_total 的最小档
        next_tier: Optional[dict] = None
        for tier in self._tiers:
            if float(tier["threshold"]) > current_total:
                next_tier = tier
                break

        if next_tier is None:
            # 已达最高档或无档位
            return BundleDiscountResult(
                current_total=current_total,
                store_totals=dict(store_totals),
                next_threshold=None,
                next_benefit=None,
                suggestion=None,
            )

        gap = float(next_tier["threshold"]) - current_total
        benefit = float(next_tier["benefit"])
        suggestion = (
            f"再凑 ¥{gap:.0f} 可跨店满减 ¥{benefit:.0f}"
            f"（{next_tier.get('name', '下一档满减')}）"
        )

        return BundleDiscountResult(
            current_total=current_total,
            store_totals=dict(store_totals),
            next_threshold=float(next_tier["threshold"]),
            next_benefit=benefit,
            suggestion=suggestion,
        )