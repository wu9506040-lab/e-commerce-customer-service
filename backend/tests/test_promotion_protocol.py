"""
Sprint 18 场景组 B · PromotionRuleService Protocol 契约测试（spec §3.5 · 6 用例）

策略：
- #1-#2 get_active_promotions：时间窗内命中 / 过期过滤
- #3-#4 check_coupon_stackable：同类型互斥 / 不同类型叠加
- #5-#6 calculate_bundle_discount：凑单建议 / 已达标无下一档
- 附加：factory 返回值 + Protocol runtime_checkable 一致性

env 兜底由 conftest.py 提供（JWT_SECRET / DATABASE_URL）。
"""
import asyncio
import datetime
from unittest.mock import patch

import pytest

from app.services.promotion.factory import get_promotion_rule_service_factory
from app.services.promotion.protocols import PromotionRuleService
from app.services.promotion.yaml_impl import YamlPromotionRuleService


def _run(coro):
    return asyncio.run(coro)


# =============================================================
# #1-#2 get_active_promotions（时间窗 + 店铺/类目过滤）
# =============================================================
def test_get_active_promotions_within_window():
    """#1 当前时间窗内的优惠全部命中（已知 active = 2：美妆 + storeA）"""
    svc = YamlPromotionRuleService()
    promos = _run(svc.get_active_promotions(1001, []))
    assert len(promos) >= 2
    promo_ids = {p.promotion_id for p in promos}
    # 已过期 PROMO_2025_EXPIRED 不在
    assert "PROMO_2025_EXPIRED" not in promo_ids
    # 未来 D11 不在（2026-11 未到）
    assert "PROMO_2026_D11_001" not in promo_ids
    assert "PROMO_2026_D11_002" not in promo_ids


def test_get_active_promotions_filter_expired():
    """#2 过期优惠被过滤（手动验证：时间窗外 → 0 命中）

    策略：把 start_time/end_time 全调到 2020 年（同一条促销），断言不出现在结果里。
    """
    svc = YamlPromotionRuleService()
    # 直接验证 _match_promotion：选一条已过期促销（去年双 11）
    from app.schemas.business import Promotion

    expired = Promotion(
        promotion_id="EXPIRED",
        name="已过期",
        type="full_reduction",
        threshold=200.0,
        benefit=30.0,
        applicable_stores=[],
        applicable_categories=[],
        start_time=datetime.datetime(2020, 1, 1),
        end_time=datetime.datetime(2020, 12, 31),
        stackable=True,
    )
    # 走真实 list（不调 _match_promotion 私有函数，避免测实现细节）
    promos = _run(svc.get_active_promotions(1001, []))
    expired_ids = {p.promotion_id for p in promos}
    assert "EXPIRED" not in expired_ids
    # 同时验证：把促销快照全改成 2020，过期促销 0 命中
    with patch.object(svc, "_promotions", [expired]):
        result = _run(svc.get_active_promotions(1001, []))
        assert result == []


# =============================================================
# #3-#4 check_coupon_stackable（互斥 + 叠加分组）
# =============================================================
def test_check_coupon_stackable_same_type_conflict():
    """#3 同类型满减券互斥 → conflicting_pairs 非空 + best_combination 只取 1 张"""
    svc = YamlPromotionRuleService()
    result = _run(svc.check_coupon_stackable(["C_FULL_50", "C_FULL_30"]))
    assert len(result.conflicting_pairs) == 1
    pair = result.conflicting_pairs[0]
    assert {pair["a"], pair["b"]} == {"C_FULL_50", "C_FULL_30"}
    assert "满减" in pair["reason"]
    # best_combination 只取 1 张（同组互斥）
    assert len(result.best_combination) == 1
    assert result.best_combination[0] in {"C_FULL_50", "C_FULL_30"}


def test_check_coupon_stackable_different_types():
    """#4 不同类型可叠加（满减 + 折扣 + 赠品）→ stackable_groups 按类型分组 + best 包含全部"""
    svc = YamlPromotionRuleService()
    result = _run(svc.check_coupon_stackable(["C_FULL_50", "C_DISC_9", "C_GIFT_X"]))
    # 满减 vs 折扣 也互斥（coupon_rules conflict_rules 第 3 条）
    assert len(result.conflicting_pairs) == 1
    pair = result.conflicting_pairs[0]
    assert {pair["a"], pair["b"]} == {"C_FULL_50", "C_DISC_9"}
    # 但 gift 与 full / disc 不互斥 → stackable_groups 按类型分 3 组
    assert len(result.stackable_groups) == 3
    assert sorted(result.best_combination) == ["C_DISC_9", "C_FULL_50", "C_GIFT_X"]


# =============================================================
# #5-#6 calculate_bundle_discount（凑单建议 + 已达标）
# =============================================================
def test_calculate_bundle_discount_suggestion():
    """#5 未达最高档 → next_threshold + suggestion 非空"""
    svc = YamlPromotionRuleService()
    result = _run(svc.calculate_bundle_discount({
        "storeA": 180, "storeB": 70,
    }))
    # current_total = 250，距 400 档差 150，距 800 档差 550 → 下一档 = 400
    assert result.current_total == 250.0
    assert result.next_threshold == 400.0
    assert result.next_benefit == 50.0
    assert result.suggestion is not None
    assert "150" in result.suggestion  # 250→400 差 150
    assert "50" in result.suggestion    # 减 50


def test_calculate_bundle_discount_reached_top_tier():
    """#6 已达最高档（≥ 800）→ next_threshold=None + suggestion=None"""
    svc = YamlPromotionRuleService()
    result = _run(svc.calculate_bundle_discount({
        "storeA": 500, "storeB": 400,   # 合计 900
    }))
    assert result.current_total == 900.0
    assert result.next_threshold is None
    assert result.next_benefit is None
    assert result.suggestion is None
    # store_totals 原样回传
    assert result.store_totals == {"storeA": 500, "storeB": 400}


# =============================================================
# Factory 一致性（额外验证，非 spec 列表但 cheap）
# =============================================================
def test_factory_returns_yaml_impl():
    """get_promotion_rule_service_factory() → YamlPromotionRuleService + runtime_checkable"""
    factory = get_promotion_rule_service_factory()
    svc = factory.get_promotion_rule_service()
    assert isinstance(svc, YamlPromotionRuleService)
    assert isinstance(svc, PromotionRuleService)  # Protocol runtime_checkable