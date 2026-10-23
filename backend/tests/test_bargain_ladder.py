"""阶梯议价价格引擎测试（V20 亮点 6：确定性让价决策）"""
from __future__ import annotations

import pytest

from app.services.xianyu.bargain_ladder import decide_offer


def test_round1_rejects():
    r = decide_offer(100.0, 1)
    assert r["strategy"] == "reject"
    assert r["price"] is None
    assert r["floor_price"] == 90.0     # 默认底价 9 折


def test_round2_concedes_3pct():
    r = decide_offer(100.0, 2)
    assert r["strategy"] == "concede"
    assert r["price"] == 97.0


def test_round3_concedes_5pct():
    r = decide_offer(100.0, 3)
    assert r["strategy"] == "concede"
    assert r["price"] == 95.0


def test_round4plus_floor_strategy():
    r = decide_offer(100.0, 4)
    assert r["strategy"] == "floor"
    assert r["price"] == 90.0           # 底价托底
    r9 = decide_offer(100.0, 9)
    assert r9["strategy"] == "floor"    # 超长议价也是托底


def test_offer_never_below_floor():
    """让幅超过底价上限 → 回落 floor 策略，绝不上穿底价。"""
    r = decide_offer(100.0, 3, max_discount=0.04)  # 底价 96 > 3 轮让价 95
    assert r["strategy"] == "floor"
    assert r["price"] == 96.0


def test_small_discount_rejects():
    """让幅不足 1 元（买家无感）→ 拒绝而不是给个无意义的小让。"""
    r = decide_offer(20.0, 2)  # 3% = 0.6 元
    assert r["strategy"] == "reject"


def test_custom_ladder_per_sku():
    r = decide_offer(100.0, 2, ladder={1: 0.0, 2: 0.08})
    assert r["strategy"] == "concede"
    assert r["price"] == 92.0


def test_instruction_has_price_guard():
    """给 LLM 的指令必须包含价格红线约束（防幻觉报价）。"""
    r = decide_offer(100.0, 2)
    assert "97.0" in r["instruction"]
    assert "禁止" in r["instruction"]


def test_invalid_price_raises():
    with pytest.raises(ValueError):
        decide_offer(0, 2)
    with pytest.raises(ValueError):
        decide_offer(-5, 2)
