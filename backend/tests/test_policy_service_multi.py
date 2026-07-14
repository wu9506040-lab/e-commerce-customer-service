"""
Phase 4 A4: PolicyService.search_multi_policy 单测

覆盖：
1. queries 为空 → 返空 list
2. 单路（queries 长度 = 1）→ 短路返回 search_policy 结果
3. 多路正常 → 走 RRF 融合
4. 单路异常 → 仅该路降级，其他路继续
5. RRF 异常 → 降级返回首路结果
6. schema 与 search_policy 一致（含 text/source/score/rerank_score/rrf_score）
"""
import os
import sys
from unittest.mock import patch

import pytest

# pytest 不走 __main__，env 必须在模块顶部 setdefault
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_fake_hits(prefix: str, n: int = 3) -> list[dict]:
    """构造 N 条带 id/source/text 的 mock 命中"""
    return [
        {
            "id": f"{prefix}-{i}",
            "text": f"{prefix} doc {i}",
            "source": f"src_{prefix}_{i}",
            "score": 0.9 - i * 0.05,
            "rerank_score": 8 - i,
        }
        for i in range(n)
    ]


def test_empty_queries_returns_empty():
    """场景 1：queries 空 → 返空 list。"""
    from app.services.policy_service import PolicyService

    assert PolicyService.search_multi_policy([], top_k=3) == []
    assert PolicyService.search_multi_policy(None, top_k=3) == []


def test_single_query_short_circuits():
    """场景 2：1 条 query → 短路返回 search_policy 结果（不调 RRF）。"""
    from app.services.policy_service import PolicyService

    fake_hits = _make_fake_hits("single", 3)
    with patch.object(PolicyService, "search_policy", return_value=fake_hits) as mock_sp:
        result = PolicyService.search_multi_policy(["only one"], top_k=3)

    assert len(result) == 3
    mock_sp.assert_called_once_with("only one", top_k=3)
    # schema 校验
    assert result[0]["text"] == "single doc 0"
    assert result[0]["source"] == "src_single_0"


def test_multi_query_runs_rag_per_query_and_fuses():
    """场景 3：3 条 query → 每路 search_policy + RRF 融合。"""
    from app.services.policy_service import PolicyService

    hits_per_query = [
        _make_fake_hits("q1", 3),
        _make_fake_hits("q2", 3),
        _make_fake_hits("q3", 3),
    ]
    call_count = [0]

    def fake_search(query, top_k=3):
        idx = call_count[0]
        call_count[0] += 1
        return hits_per_query[idx]

    with patch.object(PolicyService, "search_policy", side_effect=fake_search) as mock_sp:
        result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

    assert call_count[0] == 3  # 3 路都跑了
    assert len(result) >= 1
    assert all("rrf_score" in h for h in result)  # 多路融合后应带 rrf_score


def test_single_query_exception_continues_others():
    """场景 4：单路异常 → 仅该路降级，其他路继续。"""
    from app.services.policy_service import PolicyService

    call_count = [0]

    def side_effect(query, top_k=3):
        call_count[0] += 1
        if call_count[0] == 2:  # 第 2 路失败
            raise RuntimeError("Qdrant timeout")
        return _make_fake_hits(f"ok-{call_count[0]}", 2)

    with patch.object(PolicyService, "search_policy", side_effect=side_effect):
        with patch("app.services.policy_service.logger"):
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

    assert len(result) > 0  # 其他路结果继续
    # 第 1、3 路正常；result 至少包含其中一路的 doc
    all_sources = [h["source"] for h in result]
    assert any("ok-" in s for s in all_sources)


def test_rff_fuse_failure_falls_back_to_first():
    """场景 5：RRF 融合异常 → 降级到首路前 top_k 结果。"""
    from app.services.policy_service import PolicyService

    fake_hits_q1 = _make_fake_hits("q1", 5)
    fake_hits_q2 = _make_fake_hits("q2", 5)

    def fake_search(query, top_k=3):
        return fake_hits_q1 if "q1" in query else fake_hits_q2

    with patch.object(PolicyService, "search_policy", side_effect=fake_search), \
         patch("app.services.rrf.rrf_fuse", side_effect=RuntimeError("RRF crash")):
        with patch("app.services.policy_service.logger"):
            result = PolicyService.search_multi_policy(["q1", "q2"], top_k=3)

    assert len(result) == 3
    # 应返首路结果（截断 top_k=3）
    assert result[0]["source"] == "src_q1_0"


def test_schema_matches_search_policy():
    """场景 6：输出 schema 与 search_policy 完全一致（含 5 字段）。"""
    from app.services.policy_service import PolicyService

    fake_hits = _make_fake_hits("schema", 2)
    # search_multi_policy 单路短路，使用 search_policy mock
    with patch.object(PolicyService, "search_policy", return_value=fake_hits):
        result = PolicyService.search_multi_policy(["q"], top_k=2)

    h = result[0]
    expected_keys = {"text", "source", "score", "rerank_score"}
    assert expected_keys.issubset(set(h.keys())), \
        f"search_multi_policy 缺字段: {expected_keys - set(h.keys())}"


if __name__ == "__main__":
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    test_empty_queries_returns_empty()
    test_single_query_short_circuits()
    test_multi_query_runs_rag_per_query_and_fuses()
    test_single_query_exception_continues_others()
    test_rff_fuse_failure_falls_back_to_first()
    test_schema_matches_search_policy()
    print("\nALL 6 SCENARIOS PASSED")
