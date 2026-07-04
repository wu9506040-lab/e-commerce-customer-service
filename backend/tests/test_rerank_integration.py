"""
P1-检索 A：rerank.py 接入 PolicyService 单测

验证：
1. USE_RERANK=true → 粗排 top-15 → rerank → top-3
2. USE_RERANK=false → 保持原行为（直接 top-3）
3. rerank 失败 → 降级到粗排 top-3
4. rerank 返回顺序按 rerank_score 降序
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# 让模块能找到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_qdrant_hits(n: int) -> list[dict]:
    """构造 mock Qdrant hits（15 条候选）"""
    hits = []
    for i in range(n):
        hits.append({
            "id": f"doc-{i:03d}",
            "score": 0.9 - i * 0.01,  # Qdrant cosine 0.9 → 0.74
            "payload": {
                "text": f"这是第 {i} 条候选政策文本，含有关键词。",
                "source": f"policy_mock_{i}",
            },
        })
    return hits


def test_rerank_enabled_uses_coarse_then_fine():
    """场景 1：USE_RERANK=true → Qdrant 拉 15 条 → rerank 精排 → top-3"""
    from app.core import config

    # 强制开启 rerank
    original_use = config.settings.USE_RERANK
    original_top = config.settings.RERANK_CANDIDATE_TOP_K
    config.settings.USE_RERANK = True
    config.settings.RERANK_CANDIDATE_TOP_K = 15

    try:
        # mock Qdrant 返回 15 条
        fake_hits = _make_qdrant_hits(15)

        # mock rerank：把第 7、3、11 条排到前三（rerank_score=10/8/6）
        def fake_rerank(query, candidates, top_n=None):
            # 模拟 rerank：让 candidate[7] > candidate[3] > candidate[11]
            scores_map = {7: 10, 3: 8, 11: 6}
            for i, c in enumerate(candidates):
                c["rerank_score"] = scores_map.get(i, 1)
            sorted_cands = sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)
            return sorted_cands[:top_n] if top_n else sorted_cands

        with patch("app.services.policy_service.qdrant_search", return_value=fake_hits), \
             patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
             patch("app.services.rerank.rerank", side_effect=fake_rerank), \
             patch("app.services.bm25_index.bm25_search", return_value=[]):  # 隔离 hybrid 路径

            from app.services.policy_service import PolicyService
            results = PolicyService.search_policy("运费险怎么赔付", top_k=3)

        # 验证：返回 3 条
        assert len(results) == 3, f"应返回 3 条，实际 {len(results)}"
        # 验证：顺序按 rerank_score 降序
        scores = [r.get("rerank_score") for r in results]
        assert scores == [10, 8, 6], f"rerank 顺序错: {scores}"
        # 验证：source 对应原始候选 doc-007 / doc-003 / doc-011
        sources = [r["source"] for r in results]
        assert sources == ["policy_mock_7", "policy_mock_3", "policy_mock_11"], f"source 错: {sources}"
        # 验证：rerank_score 字段被透传
        assert all(r.get("rerank_score") is not None for r in results)
        print(f"PASS: USE_RERANK=true → 15 粗排 → 3 精排，按 rerank_score 排序")
    finally:
        config.settings.USE_RERANK = original_use
        config.settings.RERANK_CANDIDATE_TOP_K = original_top


def test_rerank_disabled_keeps_legacy_behavior():
    """场景 2：USE_RERANK=false → 保持原 Qdrant 直接 top-3 行为"""
    from app.core import config

    original_use = config.settings.USE_RERANK
    original_top = config.settings.RERANK_CANDIDATE_TOP_K
    config.settings.USE_RERANK = False

    try:
        all_hits = _make_qdrant_hits(15)

        # mock 尊重 top_k：返 top_k 条
        def fake_qdrant(query_vector, top_k, score_threshold=None, collection_name=None):
            return all_hits[:top_k]

        # 关键：rerank 不应被调用
        with patch("app.services.policy_service.qdrant_search", side_effect=fake_qdrant), \
             patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
             patch("app.services.bm25_index.bm25_search", return_value=[]), \
             patch("app.services.rerank.rerank") as mock_rerank:

            from app.services.policy_service import PolicyService
            results = PolicyService.search_policy("运费险", top_k=3)

        # rerank 不能被调用
        assert not mock_rerank.called, "USE_RERANK=false 时不应调 rerank"
        # 直接返回前 3 条（按 Qdrant 原始 cosine 排序）
        assert len(results) == 3
        assert [r["source"] for r in results] == ["policy_mock_0", "policy_mock_1", "policy_mock_2"]
        # rerank_score 应为 None
        assert all(r.get("rerank_score") is None for r in results)
        print(f"PASS: USE_RERANK=false → 直接 top-3，rerank 未调用")
    finally:
        config.settings.USE_RERANK = original_use


def test_rerank_failure_falls_back_to_coarse():
    """场景 3：rerank 抛异常 → 降级到粗排 top-3，业务不崩"""
    from app.core import config

    original_use = config.settings.USE_RERANK
    original_top = config.settings.RERANK_CANDIDATE_TOP_K
    config.settings.USE_RERANK = True
    config.settings.RERANK_CANDIDATE_TOP_K = 15

    try:
        fake_hits = _make_qdrant_hits(15)

        # rerank 抛异常
        def fake_rerank_fail(query, candidates, top_n=None):
            raise RuntimeError("Qwen API timeout")

        with patch("app.services.policy_service.qdrant_search", return_value=fake_hits), \
             patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
             patch("app.services.rerank.rerank", side_effect=fake_rerank_fail), \
             patch("app.services.bm25_index.bm25_search", return_value=[]):

            from app.services.policy_service import PolicyService
            # 不应抛异常
            results = PolicyService.search_policy("运费险", top_k=3)

        # 降级后仍返 3 条，按 Qdrant 原始 cosine 排序
        assert len(results) == 3
        assert [r["source"] for r in results] == ["policy_mock_0", "policy_mock_1", "policy_mock_2"]
        # rerank_score 字段不存在或 None
        assert all(r.get("rerank_score") is None for r in results)
        print(f"PASS: rerank 异常 → 降级到粗排 top-3，业务不崩")
    finally:
        config.settings.USE_RERANK = original_use
        config.settings.RERANK_CANDIDATE_TOP_K = original_top


def test_small_corpus_skips_rerank():
    """场景 4：粗排命中数 ≤ top_k → 不调 rerank（节省 token）"""
    from app.core import config

    original_use = config.settings.USE_RERANK
    original_top = config.settings.RERANK_CANDIDATE_TOP_K
    config.settings.USE_RERANK = True
    config.settings.RERANK_CANDIDATE_TOP_K = 15

    try:
        # Qdrant 只返 2 条（< top_k=5）
        fake_hits = _make_qdrant_hits(2)

        with patch("app.services.policy_service.qdrant_search", return_value=fake_hits), \
             patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
             patch("app.services.bm25_index.bm25_search", return_value=[]), \
             patch("app.services.rerank.rerank") as mock_rerank:

            from app.services.policy_service import PolicyService
            results = PolicyService.search_policy("运费险", top_k=5)

        # rerank 不应被调用（粗排不足 top_k 时跳过）
        assert not mock_rerank.called, f"粗排 {len(fake_hits)} < top_k={5} 时不应 rerank"
        assert len(results) == 2
        print(f"PASS: 粗排 2 条 < top_k=5 → 不调 rerank（省 token）")
    finally:
        config.settings.USE_RERANK = original_use
        config.settings.RERANK_CANDIDATE_TOP_K = original_top


def test_qdrant_returns_empty():
    """场景 5：Qdrant 返空（断路器开路 / 无命中）→ 返空 list，不调 rerank"""
    from app.services.policy_service import PolicyService

    with patch("app.services.policy_service.qdrant_search", return_value=[]), \
         patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
         patch("app.services.bm25_index.bm25_search", return_value=[]), \
         patch("app.services.rerank.rerank") as mock_rerank:

        results = PolicyService.search_policy("运费险", top_k=3)

    assert results == []
    assert not mock_rerank.called
    print(f"PASS: Qdrant 返空 → []，不调 rerank")


def test_embedding_failure_returns_empty():
    """场景 6：embed 失败 → 返空，不调 Qdrant 不调 rerank"""
    from app.services.policy_service import PolicyService

    with patch("app.core.embedding.embed_text", side_effect=Exception("embedding API down")), \
         patch("app.services.policy_service.qdrant_search") as mock_qdrant, \
         patch("app.services.rerank.rerank") as mock_rerank:

        results = PolicyService.search_policy("运费险", top_k=3)

    assert results == []
    assert not mock_qdrant.called
    assert not mock_rerank.called
    print(f"PASS: embed 失败 → []，Qdrant/rerank 都不调")


if __name__ == "__main__":
    # 配置环境变量避免 config 校验报错
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    test_rerank_enabled_uses_coarse_then_fine()
    test_rerank_disabled_keeps_legacy_behavior()
    test_rerank_failure_falls_back_to_coarse()
    test_small_corpus_skips_rerank()
    test_qdrant_returns_empty()
    test_embedding_failure_returns_empty()
    print("\nALL 6 SCENARIOS PASSED")