"""议价引擎 gift 模式测试（V21 亮点6：不二价人设的赠品梯度决策）"""
from __future__ import annotations

from app.services.xianyu.bargain_ladder import decide_gift_offer


def test_round1_rejects():
    r = decide_gift_offer(1)
    assert r["strategy"] == "reject"
    assert r["gift"] is None
    assert "禁止承诺赠品以外" in r["instruction"]


def test_round2_gifts_cable():
    r = decide_gift_offer(2)
    assert r["strategy"] == "gift"
    assert r["gift"] == "数据线"
    assert "禁止降价" in r["instruction"]


def test_round3_adds_charger():
    r = decide_gift_offer(3)
    assert r["strategy"] == "gift"
    assert r["gift"] == "18W 充电头"


def test_round4plus_final_promotion():
    r = decide_gift_offer(4)
    assert r["strategy"] == "final"
    assert "全部赠品" in r["gift"]
    r9 = decide_gift_offer(9)
    assert r9["strategy"] == "final"


def test_custom_ladder():
    r = decide_gift_offer(2, gift_ladder={1: None, 2: "钢化膜"})
    assert r["gift"] == "钢化膜"


def test_instruction_never_allows_price_cut():
    """gift/final 模式的指令必须守住不二价人设。"""
    for rd in (2, 3, 5):
        r = decide_gift_offer(rd)
        assert "降价" in r["instruction"] or "让价" in r["instruction"]
