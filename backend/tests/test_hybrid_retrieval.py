"""
P1-检索 B：BM25 + RRF 混合检索单测

测试覆盖：
1. BM25 tokenization（中文 2-gram + 英文单词）
2. BM25Okapi 评分数学正确性
3. RRF 融合数学正确性
4. 混合检索 pipeline（vector + BM25 + RRF + rerank）
5. USE_HYBRID=false → 走纯 vector 路径（无 BM25 调用）
6. BM25 索引构建失败 → 降级到纯 vector
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ====================== Tokenization ======================

def test_tokenize_chinese_bigram():
    """场景 1：中文 2-gram 切词"""
    from app.services.bm25_index import _tokenize

    tokens = _tokenize("无理由退货")
    # 应包含单字
    assert "无" in tokens and "理" in tokens and "由" in tokens and "退" in tokens
    # 应包含 2-gram（顺序相邻的 2 字）
    assert "无理" in tokens
    assert "理由" in tokens
    assert "由退" in tokens
    assert "退货" in tokens
    print(f"PASS: 中文切词 → {tokens}")


def test_tokenize_english_words():
    """场景 2：英文/数字按单词切"""
    from app.services.bm25_index import _tokenize

    tokens = _tokenize("ZP2 Pro Max 续航")
    assert "zp2" in tokens
    assert "pro" in tokens
    assert "max" in tokens
    # 中文字符也应被切
    assert "续航" in tokens
    print(f"PASS: 英文+中文混切 → {tokens}")


def test_tokenize_empty():
    """场景 3：空字符串 → 空 list"""
    from app.services.bm25_index import _tokenize
    assert _tokenize("") == []
    assert _tokenize("   ") == []
    print("PASS: 空字符串 → []")


# ====================== BM25 Scoring ======================

def test_bm25_basic_scoring():
    """场景 4：BM25 评分数学正确（命中词的 doc 分数高于未命中）"""
    from app.services.bm25_index import BM25Okapi

    # query 含 "退货" 应让含 "退货" 的 doc 排前
    corpus = [
        _tokenize("退货政策说明"),         # 含 "退货"
        _tokenize("运费险常见问题"),       # 不含 "退货"
        _tokenize("七天无理由退货"),       # 含 "退货"
    ]
    bm25 = BM25Okapi(corpus)
    scores = bm25.score(_tokenize("退货"))

    # 含 "退货" 的 doc0/doc2 分数 > 不含 "退货" 的 doc1
    assert scores[0] > scores[1], f"含查询词 doc0={scores[0]} 应 > 不含 doc1={scores[1]}"
    assert scores[2] > scores[1], f"含查询词 doc2={scores[2]} 应 > 不含 doc1={scores[1]}"
    # doc1 分数应为 0（无任何 query token 命中）
    assert scores[1] == 0.0, f"无命中 doc1 分数应为 0，实际 {scores[1]}"
    print(f"PASS: BM25 排序正确: doc0={scores[0]:.3f}, doc2={scores[2]:.3f} > doc1=0")


def test_bm25_idf_excludes_stopwords():
    """场景 5：所有文档都含的词 → IDF 低，分数贡献小"""
    from app.services.bm25_index import BM25Okapi

    # 3 篇文档都含 "的"，query "的" 不应让分数有明显差异
    corpus = [
        _tokenize("退货的政策"),
        _tokenize("保修的政策"),
        _tokenize("运费的政策"),
    ]
    bm25 = BM25Okapi(corpus)
    scores = bm25.score(_tokenize("的"))
    # IDF 低 → 三 doc 分数应该都低（高频词无判别力）
    # IDF = log((3-3+0.5)/(3+0.5) + 1) = log(1.143) ≈ 0.134
    # 乘以 BM25 归一化项（< 1.5），总分 < 0.5
    assert all(s < 0.5 for s in scores), f"高频词 IDF 应低，实际 {scores}"
    # 三 doc 分数应相等（都被长度归一化影响，但相同 dl 时相同）
    # 这里 dl 不同，但分数差距应很小
    assert max(scores) - min(scores) < 0.1, f"高频词应分数接近，实际 {scores}"
    print(f"PASS: 高频词 IDF 低，分数近: {scores}")


# ====================== RRF ======================

def _tokenize(text: str):
    from app.services.bm25_index import _tokenize as _t
    return _t(text)


def test_rrf_basic_fusion():
    """场景 6：RRF 融合数学正确"""
    from app.services.rrf import rrf_fuse

    # vector 路：doc-A 第 1，doc-B 第 2
    vector_results = [
        {"id": "A", "text": "doc A", "source": "src_a", "score": 0.95},
        {"id": "B", "text": "doc B", "source": "src_b", "score": 0.85},
    ]
    # BM25 路：doc-B 第 1，doc-A 第 2
    bm25_results = [
        {"id": "B", "text": "doc B", "source": "src_b", "score": 5.2},
        {"id": "A", "text": "doc A", "source": "src_a", "score": 3.8},
    ]

    fused = rrf_fuse([vector_results, bm25_results], k=60)

    # doc-A: 1/(60+1) + 1/(60+2) = 1/61 + 1/62
    # doc-B: 1/(60+2) + 1/(60+1) = 1/62 + 1/61
    # 两者应相等（因为互换 rank）
    assert abs(fused[0]["rrf_score"] - fused[1]["rrf_score"]) < 1e-6, \
        f"互换 rank 的 doc 应有相同 rrf_score: {fused[0]['rrf_score']} vs {fused[1]['rrf_score']}"
    expected = 1/61 + 1/62
    assert abs(fused[0]["rrf_score"] - expected) < 1e-4, \
        f"RRF 分数应 ≈ {expected:.4f}，实际 {fused[0]['rrf_score']:.4f}"
    print(f"PASS: RRF 融合正确，两路互换 rank 后分数相等: {fused[0]['rrf_score']:.4f}")


def test_rrf_multi_source_boost():
    """场景 7：两路都命中的 doc > 仅一路命中的 doc"""
    from app.services.rrf import rrf_fuse

    vector_results = [
        {"id": "A", "text": "doc A", "source": "src_a"},
        {"id": "B", "text": "doc B", "source": "src_b"},  # 仅 vector
        {"id": "C", "text": "doc C", "source": "src_c"},  # 仅 vector
    ]
    bm25_results = [
        {"id": "A", "text": "doc A", "source": "src_a"},  # 双命中
        {"id": "D", "text": "doc D", "source": "src_d"},  # 仅 bm25
    ]

    fused = rrf_fuse([vector_results, bm25_results], k=60)

    # doc-A 双命中 → 排第一
    assert fused[0]["id"] == "A", f"双命中 doc-A 应排第一，实际 {fused[0]['id']}"
    assert len(fused[0]["source_ranks"]) == 2, "source_ranks 应记录两路"
    print(f"PASS: 双命中 doc 优先: top1={fused[0]['id']} (双命中)")


def test_rrf_empty_inputs():
    """场景 8：空输入 → 空输出"""
    from app.services.rrf import rrf_fuse
    assert rrf_fuse([]) == []
    assert rrf_fuse([[]]) == []
    print("PASS: 空输入 → []")


# ====================== Hybrid Pipeline ======================

def test_hybrid_enabled_uses_vector_and_bm25():
    """场景 9：USE_HYBRID=true → vector + BM25 都调，RRF 融合"""
    from app.core import config
    from app.services import bm25_index

    # 重置索引缓存
    bm25_index.invalidate()

    original_hybrid = config.settings.USE_HYBRID_BM25
    original_rerank = config.settings.USE_RERANK
    original_top = config.settings.RERANK_CANDIDATE_TOP_K
    config.settings.USE_HYBRID_BM25 = True
    config.settings.USE_RERANK = False  # 关掉 rerank 简化测试
    config.settings.RERANK_CANDIDATE_TOP_K = 15

    try:
        # mock vector 返 3 条（id=A/B/C）
        vector_hits = [
            {"id": "A", "score": 0.95, "payload": {"text": "退货政策A", "source": "src_a"}},
            {"id": "B", "score": 0.85, "payload": {"text": "保修政策B", "source": "src_b"}},
            {"id": "C", "score": 0.75, "payload": {"text": "运费政策C", "source": "src_c"}},
        ]

        # mock BM25 返 2 条（id=B/A，与 vector 有重叠）
        bm25_hits = [
            {"id": "B", "text": "保修政策B", "source": "src_b", "score": 5.0},
            {"id": "A", "text": "退货政策A", "source": "src_a", "score": 3.0},
        ]

        with patch("app.services.policy_service.qdrant_search", return_value=vector_hits), \
             patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
             patch("app.services.bm25_index.bm25_search", return_value=bm25_hits):

            from app.services.policy_service import PolicyService
            results = PolicyService.search_policy("退货", top_k=3)

        # 验证：vector + BM25 都调了（通过 RRF 融合）
        # 由于 A 和 B 都在两路 top，应排在前面（双命中）
        assert len(results) == 3
        top_ids = [r["source"] for r in results[:2]]  # top 2 应是 A 和 B（双命中）
        assert "src_a" in top_ids and "src_b" in top_ids, \
            f"A/B 双命中应在 top 2，实际 {top_ids}"
        # rrf_score 字段应存在
        assert all(r.get("rrf_score") is not None for r in results), "混合检索结果应带 rrf_score"
        print(f"PASS: hybrid 启用 → 双命中 A/B 优先: top2={top_ids}")
    finally:
        config.settings.USE_HYBRID_BM25 = original_hybrid
        config.settings.USE_RERANK = original_rerank
        config.settings.RERANK_CANDIDATE_TOP_K = original_top


def test_hybrid_disabled_skips_bm25():
    """场景 10：USE_HYBRID=false → 不调 BM25，纯 vector 路径"""
    from app.core import config

    original_hybrid = config.settings.USE_HYBRID_BM25
    config.settings.USE_HYBRID_BM25 = False

    try:
        vector_hits = [
            {"id": "A", "score": 0.95, "payload": {"text": "退货政策A", "source": "src_a"}},
            {"id": "B", "score": 0.85, "payload": {"text": "保修政策B", "source": "src_b"}},
        ]

        with patch("app.services.policy_service.qdrant_search", return_value=vector_hits), \
             patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
             patch("app.services.bm25_index.bm25_search") as mock_bm25:

            from app.services.policy_service import PolicyService
            results = PolicyService.search_policy("退货", top_k=3)

        # BM25 不应被调用
        assert not mock_bm25.called, "USE_HYBRID=false 时不应调 BM25"
        # rrf_score 应为 None
        assert all(r.get("rrf_score") is None for r in results)
        print("PASS: hybrid 关闭 → 纯 vector，BM25 未调用")
    finally:
        config.settings.USE_HYBRID_BM25 = original_hybrid


def test_bm25_failure_falls_back_to_vector():
    """场景 11：BM25 抛异常 → 降级到纯 vector 路径，业务不崩"""
    from app.core import config

    original_hybrid = config.settings.USE_HYBRID_BM25
    original_rerank = config.settings.USE_RERANK
    config.settings.USE_HYBRID_BM25 = True
    config.settings.USE_RERANK = False

    try:
        vector_hits = [
            {"id": "A", "score": 0.95, "payload": {"text": "退货", "source": "src_a"}},
            {"id": "B", "score": 0.85, "payload": {"text": "保修", "source": "src_b"}},
        ]

        with patch("app.services.policy_service.qdrant_search", return_value=vector_hits), \
             patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
             patch("app.services.bm25_index.bm25_search", side_effect=RuntimeError("Qdrant down")):

            from app.services.policy_service import PolicyService
            # 不应抛异常
            results = PolicyService.search_policy("退货", top_k=3)

        # 降级后仍返 vector 结果
        assert len(results) == 2
        assert [r["source"] for r in results] == ["src_a", "src_b"]
        print("PASS: BM25 异常 → 降级到纯 vector，业务不崩")
    finally:
        config.settings.USE_HYBRID_BM25 = original_hybrid
        config.settings.USE_RERANK = original_rerank


def test_hybrid_then_rerank_chain():
    """场景 12：hybrid → rerank 链路（vector + BM25 → RRF → rerank → top-3）"""
    from app.core import config
    from app.services import bm25_index

    bm25_index.invalidate()

    original_hybrid = config.settings.USE_HYBRID_BM25
    original_rerank = config.settings.USE_RERANK
    original_top = config.settings.RERANK_CANDIDATE_TOP_K
    config.settings.USE_HYBRID_BM25 = True
    config.settings.USE_RERANK = True
    config.settings.RERANK_CANDIDATE_TOP_K = 15

    try:
        # vector 返 15 条
        vector_hits = [
            {"id": f"v{i}", "score": 0.9 - i*0.01, "payload": {"text": f"vector doc {i}", "source": f"src_v{i}"}}
            for i in range(15)
        ]
        # BM25 返 15 条（重叠小，便于看融合效果）
        bm25_hits = [
            {"id": f"b{i}", "text": f"bm25 doc {i}", "source": f"src_b{i}", "score": 5.0 - i*0.1}
            for i in range(15)
        ]

        def fake_rerank(query, candidates, top_n=None):
            # rerank 把第 5 条排到第一
            for c in candidates:
                c["rerank_score"] = 1
            if len(candidates) > 5:
                candidates[5]["rerank_score"] = 10
            sorted_cands = sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)
            return sorted_cands[:top_n] if top_n else sorted_cands

        with patch("app.services.policy_service.qdrant_search", return_value=vector_hits), \
             patch("app.core.embedding.embed_text", return_value=[0.0] * 1024), \
             patch("app.services.bm25_index.bm25_search", return_value=bm25_hits), \
             patch("app.services.rerank.rerank", side_effect=fake_rerank):

            from app.services.policy_service import PolicyService
            results = PolicyService.search_policy("混合检索测试", top_k=3)

        # rerank 后应返 3 条
        assert len(results) == 3
        # rerank_score 字段存在
        assert all(r.get("rerank_score") is not None for r in results)
        # rrf_score 字段也存在（说明走的是 hybrid 路径）
        assert all(r.get("rrf_score") is not None for r in results), \
            "hybrid → rerank 链路结果应同时带 rrf_score 和 rerank_score"
        print(f"PASS: hybrid → rerank 链路正常: 3 条结果，同时有 rrf_score 和 rerank_score")
    finally:
        config.settings.USE_HYBRID_BM25 = original_hybrid
        config.settings.USE_RERANK = original_rerank
        config.settings.RERANK_CANDIDATE_TOP_K = original_top


if __name__ == "__main__":
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    test_tokenize_chinese_bigram()
    test_tokenize_english_words()
    test_tokenize_empty()
    test_bm25_basic_scoring()
    test_bm25_idf_excludes_stopwords()
    test_rrf_basic_fusion()
    test_rrf_multi_source_boost()
    test_rrf_empty_inputs()
    test_hybrid_enabled_uses_vector_and_bm25()
    test_hybrid_disabled_skips_bm25()
    test_bm25_failure_falls_back_to_vector()
    test_hybrid_then_rerank_chain()
    print("\nALL 12 SCENARIOS PASSED")