"""黄金集 MANIFEST 防篡改测试（V21-EV 治理）。

覆盖：冻结→PASS / 改 expected→精确定位 / 删题 / 新增题 /
MANIFEST 缺失 / 格式无关（重排缩进不影响哈希）/ 发版后恢复 PASS。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.eval.golden_manifest import freeze, verify_manifest


@pytest.fixture()
def evalset(tmp_path: Path) -> Path:
    """最小黄金集：3 题（含一个场景 id 型 + 一个 query 型文件）。"""
    root = tmp_path
    (root / "data").mkdir()
    (root / "data" / "eval_a.json").write_text(json.dumps([
        {"scenario_id": "S01", "expected": {"intent": "policy_query"}},
        {"scenario_id": "S02", "expected": {"intent": "product_query"}},
    ], ensure_ascii=False), encoding="utf-8")
    (root / "data" / "eval_b.json").write_text(json.dumps([
        {"query": "能退吗", "relevant_doc_id": "doc-1"},
    ], ensure_ascii=False), encoding="utf-8")
    files = ["data/eval_a.json", "data/eval_b.json"]
    freeze(root, files, version="v1.0")
    return root


def _read(root, rel):
    return json.loads((root / rel).read_text(encoding="utf-8"))


def _write(root, rel, data):
    (root / rel).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_fresh_freeze_passes(evalset):
    ok, violations = verify_manifest(evalset)
    assert ok and violations == []


def test_expected_tamper_detected_precisely(evalset):
    """改 expected → 只报被改的那题（不误报其他题被删）。"""
    data = _read(evalset, "data/eval_a.json")
    data[0]["expected"]["intent"] = "order_query"       # 篡改
    _write(evalset, "data/eval_a.json", data)

    ok, violations = verify_manifest(evalset)
    assert not ok
    assert len(violations) == 1
    assert "S01" in violations[0] and "内容被修改" in violations[0]


def test_deleted_case_detected(evalset):
    data = _read(evalset, "data/eval_a.json")
    data.pop(1)                                          # 删 S02
    _write(evalset, "data/eval_a.json", data)

    ok, violations = verify_manifest(evalset)
    assert not ok
    assert any("S02" in v and "删除" in v for v in violations)


def test_added_case_detected(evalset):
    data = _read(evalset, "data/eval_a.json")
    data.append({"scenario_id": "S99", "expected": {}})
    _write(evalset, "data/eval_a.json", data)

    ok, violations = verify_manifest(evalset)
    assert not ok
    assert any("S99" in v and "新增" in v for v in violations)


def test_missing_manifest_fails(evalset):
    (evalset / "data" / "evalsets" / "MANIFEST.json").unlink()  # 删掉清单
    ok, violations = verify_manifest(evalset)
    assert not ok
    assert any("MANIFEST 不存在" in v for v in violations)


def test_format_change_is_ignored(evalset):
    """重排缩进 / 键序不影响哈希（canonical 规范化）——改格式不算改内容。"""
    data = _read(evalset, "data/eval_a.json")
    data.reverse()                                       # 题目顺序调换（内容集不变）
    _write(evalset, "data/eval_a.json", data)

    ok, violations = verify_manifest(evalset)
    assert ok, f"顺序调换不应报违规: {violations}"


def test_refreeze_after_legal_change(evalset):
    """合法变更 → 重新 freeze → PASS（发版流程闭环）。"""
    data = _read(evalset, "data/eval_a.json")
    data[0]["expected"]["intent"] = "order_query"        # 合法变更
    _write(evalset, "data/eval_a.json", data)
    ok, _ = verify_manifest(evalset)
    assert not ok                                        # 未发版前 FAIL

    freeze(evalset, version="v1.1")                      # 发版
    ok, violations = verify_manifest(evalset)
    assert ok and violations == []
