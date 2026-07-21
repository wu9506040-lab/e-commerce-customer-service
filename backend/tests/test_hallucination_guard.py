"""M14 V10-D：synthesize_answer 后置反幻觉校验单测。"""

from app.services.validation.hallucination_guard import post_synthesize_check


# =============================================================
# fake_amount：替换为真实 total_amount
# =============================================================
def test_fake_amount_is_replaced_with_real_amount():
    """M14-0045 主场景：LLM 输出 "54 元"，真实金额 322.21。"""
    text = "您的订单金额是 54 元，可以退款。"
    order_info = {"order_no": "ORD20260718001", "total_amount": 322.21}

    cleaned, hits = post_synthesize_check(text, order_info)

    assert "54" not in cleaned
    assert "322.21" in cleaned
    assert any(h["type"] == "fake_amount_replaced" for h in hits)
    assert any(h.get("old") == "54" for h in hits)


def test_real_amount_unchanged_returns_no_hits():
    cleaned, hits = post_synthesize_check(
        "订单金额是 ¥322.21 元",
        {"order_no": "ORD20260718001", "total_amount": 322.21},
    )

    assert cleaned == "订单金额是 ¥322.21 元"
    assert hits == []


def test_small_amount_below_threshold_skipped():
    """金额 < 30 视为非订单金额（如 7天/24小时），不替换。"""
    cleaned, hits = post_synthesize_check(
        "退款时效是 7 天无理由",
        {"order_no": "ORD20260718001", "total_amount": 322.21},
    )

    assert cleaned == "退款时效是 7 天无理由"
    assert hits == []


def test_amount_with_currency_prefix_replaced():
    text = "应退 ¥54 给您。"
    order_info = {"order_no": "ORD20260718001", "total_amount": 322.21}

    cleaned, hits = post_synthesize_check(text, order_info)

    assert "¥322.21" in cleaned or "322.21" in cleaned
    assert "54" not in cleaned


def test_integer_real_amount_no_trailing_zero():
    """真实金额 300 → 显示 "300"，不显示 "300.00"。"""
    cleaned, hits = post_synthesize_check(
        "退款 199 元",
        {"order_no": "ORD20260718001", "total_amount": 300.0},
    )

    assert "300" in cleaned
    assert "300.00" not in cleaned
    assert "199" not in cleaned


# =============================================================
# fake_order_no：替换为真实；缺失则剥离
# =============================================================
def test_fake_order_no_replaced_when_real_exists():
    text = "您的订单 ORD99999999999 已经发货。"
    order_info = {"order_no": "ORD20260718001", "total_amount": 322.21}

    cleaned, hits = post_synthesize_check(text, order_info)

    assert "ORD99999999999" not in cleaned
    assert "ORD20260718001" in cleaned
    assert any(h["type"] == "fake_order_no_replaced" for h in hits)


def test_fake_order_no_stripped_when_no_real_order():
    """M14-0070 主场景：order_info 为空（用户输入不存在的单号），剥离而非凭空编造。"""
    text = "ORD99999999999 怎么退款流程？"
    order_info = {}

    cleaned, hits = post_synthesize_check(text, order_info)

    assert "ORD99999999999" not in cleaned
    assert any(h["type"] == "fake_order_no_stripped" for h in hits)


def test_real_order_no_unchanged_returns_no_hits():
    text = "您的订单 ORD20260718001 已签收。"
    order_info = {"order_no": "ORD20260718001", "total_amount": 322.21, "status_zh": "已签收"}

    cleaned, hits = post_synthesize_check(text, order_info)

    assert cleaned == text
    assert hits == []


# =============================================================
# 边界：order_info 各种形态
# =============================================================
def test_none_order_info_returns_text_unchanged():
    """无 order_info 上下文时，不做任何替换/剥离。"""
    text = "请提供订单号。"
    cleaned, hits = post_synthesize_check(text, None)

    assert cleaned == text
    assert hits == []


def test_empty_text_returns_empty():
    cleaned, hits = post_synthesize_check("", {"order_no": "X", "total_amount": 100})

    assert cleaned == ""
    assert hits == []


def test_no_total_amount_skips_amount_check():
    """order_info 缺 total_amount（仅用于 escalate 等场景），跳过金额检查。"""
    text = "退款金额是 100 元。"
    order_info = {"order_no": "ORD20260718001"}

    cleaned, hits = post_synthesize_check(text, order_info)

    assert cleaned == text
    assert hits == []


# =============================================================
# 组合命中
# =============================================================
def test_combined_amount_and_order_no_both_replaced():
    text = "订单 ORD99999999999 已签收，金额 99 元。"
    order_info = {"order_no": "ORD20260718001", "total_amount": 322.21}

    cleaned, hits = post_synthesize_check(text, order_info)

    types = {h["type"] for h in hits}
    assert "fake_amount_replaced" in types
    assert "fake_order_no_replaced" in types
    assert "ORD99999999999" not in cleaned
    assert "322.21" in cleaned


def test_logger_emits_warning_on_hits(caplog):
    import logging
    text = "订单 ORD99999999999 已发货。"
    order_info = {"order_no": "ORD20260718001"}

    with caplog.at_level(logging.WARNING, logger="app.services.validation.hallucination_guard"):
        _cleaned, hits = post_synthesize_check(text, order_info)

    assert hits
    assert any("post_synth_hallucination_fix" in rec.message for rec in caplog.records)


# =============================================================
# V11-B：fake_status 业务层硬替换（commit ref · M14-0042/0046/0049 真实话术根因）
# =============================================================
def test_fake_status_replaced_when_not_match_real(caplog):
    """V11-B 主场景：LLM 输出"已签收"，真实状态 shipped（运输中）→ 替换为「您的订单当前状态是:运输中」。

    历史背景：V10-D fake_status 仅 warning,V11-B 升级为业务层硬替换,
    补齐与 fake_amount/fake_order_no 的"prompt + 业务层"双重防护。
    """
    import logging
    text = "您的订单已签收,感谢您选择本店。"
    order_info = {"order_no": "ORD20260718001", "total_amount": 322.21, "status_zh": "运输中"}

    with caplog.at_level(logging.WARNING, logger="app.services.validation.hallucination_guard"):
        cleaned, hits = post_synthesize_check(text, order_info)

    assert "已签收" not in cleaned
    assert "您的订单当前状态是:运输中" in cleaned
    assert any(h["type"] == "fake_status_replaced" and h["old"] == "已签收" for h in hits)


def test_real_status_unchanged_returns_no_hits():
    """LLM 输出与真实状态一致（已签收 = 已签收）→ 不替换,不计入 hit。"""
    text = "您的订单已签收,感谢您选择本店。"
    order_info = {"order_no": "ORD20260718001", "total_amount": 322.21, "status_zh": "已签收"}

    cleaned, hits = post_synthesize_check(text, order_info)

    assert cleaned == text
    assert not any(h["type"] == "fake_status_replaced" for h in hits)


def test_fake_status_stripped_when_no_real_status():
    """M14-0070 同根场景：order_info 无 status_zh（invalid_order 路径）→ 降级剥离。

    与 fake_order_no 的"缺失则剥离"行为对齐(避免凭空编造)。
    """
    text = "ORD99999999999 已签收,怎么退款？"
    order_info = {"order_no": "", "total_amount": 322.21}  # status_zh 缺失

    cleaned, hits = post_synthesize_check(text, order_info)

    assert "已签收" not in cleaned
    assert any(h["type"] == "fake_status_stripped" and h["old"] == "已签收" for h in hits)


def test_status_pattern_does_not_split_compound_words():
    """状态词正则按长度倒序排序,避免"已签收"被误拆成"已"+"签收"等边界 case。"""
    from app.services.validation.hallucination_guard import _STATUS_PATTERN

    text = "订单已完成,目前状态已签收,等待退款"
    found = _STATUS_PATTERN.findall(text)

    # 应识别出 2 个完整状态词("已完成" + "已签收"),不出现单字
    assert "已完成" in found
    assert "已签收" in found
    assert "已" not in found  # 单字不被误识别


def test_combined_fake_status_amount_order_no_all_replaced():
    """V11-B 集成场景:fake_status + fake_amount + fake_order_no 同时命中。"""
    text = "订单 ORD99999999999 已签收,金额 99 元。"
    order_info = {
        "order_no": "ORD20260718001",
        "total_amount": 322.21,
        "status_zh": "运输中",
    }

    cleaned, hits = post_synthesize_check(text, order_info)

    types = {h["type"] for h in hits}
    assert "fake_amount_replaced" in types
    assert "fake_order_no_replaced" in types
    assert "fake_status_replaced" in types
    assert "已签收" not in cleaned
    assert "ORD99999999999" not in cleaned
    assert "322.21" in cleaned
    assert "您的订单当前状态是:运输中" in cleaned


def test_hallucination_replace_fake_status_off_falls_back_to_warning(caplog):
    """灰度开关 HALLUCINATION_REPLACE_FAKE_STATUS=false → 退回 V10-D 行为:fake_status 仅 warning,不替换。

    通过 monkeypatch 修改模块级常量来测试(不依赖 reload)。
    """
    import logging
    from app.services.validation import hallucination_guard as hg

    text = "您的订单已签收,感谢您选择本店。"
    order_info = {"order_no": "ORD20260718001", "total_amount": 322.21, "status_zh": "运输中"}

    original = hg.HALLUCINATION_REPLACE_FAKE_STATUS
    try:
        hg.HALLUCINATION_REPLACE_FAKE_STATUS = False  # 关闭业务层替换
        with caplog.at_level(logging.WARNING, logger="app.services.validation.hallucination_guard"):
            cleaned, hits = post_synthesize_check(text, order_info)

        # 关闭后:不替换,文本保留"已签收";fake_status 不出现在 hits
        assert cleaned == text
        assert not any(h["type"] == "fake_status_replaced" for h in hits)
        assert not any(h["type"] == "fake_status_stripped" for h in hits)
    finally:
        hg.HALLUCINATION_REPLACE_FAKE_STATUS = original  # 恢复,防污染下游测试
