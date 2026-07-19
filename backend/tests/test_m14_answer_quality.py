"""M14 V10-C：政策覆盖率空白归一化测试。"""

from scripts.m14_validation.answer_quality import evaluate_coverage


def test_agent_whitespace_variant_matches_canonical_keyword():
    report = evaluate_coverage(
        agent_output="我们会在 24 小时 内处理",
        ref_answer="我们会在24小时内处理",
        scenario_type="refund",
    )

    assert report.coverage_rate == 1.0
    assert report.ref_keywords == ["24小时"]
    assert report.agent_keywords == ["24小时"]
    assert report.missing_keywords == []


def test_reference_whitespace_variant_is_still_measurable():
    report = evaluate_coverage(
        agent_output="支持7天无理由退货",
        ref_answer="支持 7 天 无 理 由 退货",
        scenario_type="refund",
    )

    assert report.coverage_rate == 1.0
    assert report.ref_keywords == ["7天无理由"]
    assert report.agent_keywords == ["7天无理由"]


def test_newline_and_tab_variants_match():
    report = evaluate_coverage(
        agent_output="预计24\n小时内完成",
        ref_answer="预计24\t小时内完成",
        scenario_type="logistics",
    )

    assert report.coverage_rate == 1.0
    assert report.agent_keywords == ["24小时"]


def test_normalization_does_not_create_unrelated_match():
    report = evaluate_coverage(
        agent_output="我们会尽快处理",
        ref_answer="预计24小时内完成",
        scenario_type="refund",
    )

    assert report.coverage_rate == 0.0
    assert report.agent_keywords == []
    assert report.missing_keywords == ["24小时"]


def test_reference_without_policy_keyword_remains_unscored():
    report = evaluate_coverage(
        agent_output="我们会尽快处理",
        ref_answer="请耐心等待后续通知",
        scenario_type="refund",
    )

    assert report.coverage_rate is None
    assert report.ref_keywords == []
