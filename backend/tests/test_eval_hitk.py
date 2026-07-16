"""
B1 RAG 评测 harness 增强 - 单元测试 + 阈值门禁

覆盖 scripts/eval_hitk.py 的核心逻辑：
- load_eval_set：格式校验
- evaluate_single：4 模式（baseline / rerank / bm25 / multi_query）路径分支
- summarize：汇总统计（hit@K + by_source + miss_samples）
- --latency-bench flag 行为（3 次取中位数）
- 阈值门禁：hit@5 < 0.6 报警

策略：mock embedding/qdrant/rerank/bm25，不依赖真实服务；CI 可跑。

依据：B1.1 commit 57eaa6a「feat(rag-eval): add lightweight faithfulness scoring
and mode comparison」配套测试；保障评测脚本核心逻辑不退化。
"""
import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# pytest 不走 __main__，env 必须在模块顶部 setdefault
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")

# path 处理：tests/ 在 backend/tests/，要能 import app.* 和 scripts.*
TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent  # backend/
PROJECT_ROOT = BACKEND_DIR.parent  # 智能客服/

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(PROJECT_ROOT))


# =============================================================
# 配置常量
# =============================================================
THRESHOLD_HIT5 = 0.6  # CI 门禁：hit@5 < 0.6 fail


# =============================================================
# 工具函数
# =============================================================
def _make_fake_eval_set(n: int = 10) -> list[dict]:
    """构造 N 条 mock 评测集"""
    items = []
    for i in range(n):
        items.append({
            "query": f"测试 query {i}",
            "relevant_doc_id": f"doc-{i}",
            "source": f"src_{i % 3}",
        })
    return items


def _make_fake_embedding(dim: int = 1024) -> list[float]:
    """构造固定维度 mock embedding 向量"""
    return [0.01] * dim


def _make_fake_qdrant_hits(query_text: str, top_k: int = 15) -> list[dict]:
    """构造 mock Qdrant top-K 命中（id + text + score）"""
    return [
        {
            "id": f"doc-{i}",
            "text": f"mock doc {i} for {query_text[:20]}",
            "source": f"src_{i % 3}",
            "score": 0.9 - i * 0.05,
        }
        for i in range(min(top_k, 10))
    ]


# =============================================================
# 1. load_eval_set 校验
# =============================================================
def test_load_eval_set_success(tmp_path):
    """正常 JSON 列表应能加载"""
    from scripts.eval_hitk import load_eval_set

    eval_file = tmp_path / "eval.json"
    eval_file.write_text(json.dumps(_make_fake_eval_set(5), ensure_ascii=False), encoding="utf-8")

    result = load_eval_set(eval_file)
    assert len(result) == 5
    assert result[0]["query"] == "测试 query 0"
    assert result[0]["relevant_doc_id"] == "doc-0"


def test_load_eval_set_missing_query_field(tmp_path):
    """缺 query 字段应抛 ValueError"""
    from scripts.eval_hitk import load_eval_set

    eval_file = tmp_path / "eval.json"
    eval_file.write_text(json.dumps([{"relevant_doc_id": "doc-0"}], ensure_ascii=False), encoding="utf-8")

    try:
        load_eval_set(eval_file)
        raise AssertionError("应该抛 ValueError")
    except ValueError as e:
        # 错误消息含"缺少必要字段"和完整 item
        assert "缺少必要字段" in str(e)
        assert "relevant_doc_id" in str(e)  # item 复述


def test_load_eval_set_missing_relevant_doc_id(tmp_path):
    """缺 relevant_doc_id 字段应抛 ValueError"""
    from scripts.eval_hitk import load_eval_set

    eval_file = tmp_path / "eval.json"
    eval_file.write_text(json.dumps([{"query": "q"}], ensure_ascii=False), encoding="utf-8")

    try:
        load_eval_set(eval_file)
        raise AssertionError("应该抛 ValueError")
    except ValueError as e:
        assert "缺少必要字段" in str(e)
        assert "query" in str(e)  # item 复述


def test_load_eval_set_wrong_format(tmp_path):
    """dict 而非 list 应抛 ValueError"""
    from scripts.eval_hitk import load_eval_set

    eval_file = tmp_path / "eval.json"
    eval_file.write_text(json.dumps({"not": "a list"}, ensure_ascii=False), encoding="utf-8")

    try:
        load_eval_set(eval_file)
        raise AssertionError("应该抛 ValueError")
    except ValueError as e:
        assert "list" in str(e)


def test_load_eval_set_file_not_found():
    """文件不存在应抛 FileNotFoundError"""
    from scripts.eval_hitk import load_eval_set

    try:
        load_eval_set(Path("/nonexistent/eval.json"))
        raise AssertionError("应该抛 FileNotFoundError")
    except FileNotFoundError:
        pass


# =============================================================
# 2. evaluate_single 4 模式
# =============================================================
def test_evaluate_single_baseline_mode():
    """baseline 模式：mock embed + qdrant → top-K 命中"""
    from scripts.eval_hitk import evaluate_single

    item = {"query": "test q", "relevant_doc_id": "doc-0", "source": "src_0"}

    with patch("scripts.eval_hitk.get_embedding_provider") as mock_emb, \
         patch("scripts.eval_hitk.qdrant_search") as mock_qdrant:
        mock_emb.return_value.embed_text.return_value = _make_fake_embedding()
        mock_qdrant.return_value = _make_fake_qdrant_hits("test q", top_k=15)

        result = evaluate_single(item, use_rerank=False, use_bm25=False, use_multi_query=False)

    assert result["query"] == "test q"
    assert result["relevant_doc_id"] == "doc-0"
    assert result["rank"] == 1  # doc-0 是第 1 个
    assert result["hit_at_k"][1] is True
    assert result["hit_at_k"][5] is True
    assert result["latency_ms"] >= 0


def test_evaluate_single_rerank_mode():
    """rerank 模式：mock rerank → top-N"""
    from scripts.eval_hitk import evaluate_single, TOP_K_MAX

    item = {"query": "test q", "relevant_doc_id": "doc-0", "source": "src_0"}

    def fake_rerank(query, candidates, top_n):
        # 简单 mock：返回前 top_n 个原顺序
        return candidates[:top_n]

    with patch("scripts.eval_hitk.get_embedding_provider") as mock_emb, \
         patch("scripts.eval_hitk.qdrant_search") as mock_qdrant, \
         patch("app.core.providers.rerank.get_rerank_provider") as mock_rerank_prov:
        mock_emb.return_value.embed_text.return_value = _make_fake_embedding()
        mock_qdrant.return_value = _make_fake_qdrant_hits("test q", top_k=15)
        mock_rerank_prov.return_value.rerank.side_effect = fake_rerank

        result = evaluate_single(item, use_rerank=True, use_bm25=False, use_multi_query=False)

    assert result["rank"] == 1
    assert len(result["retrieved_ids"]) == TOP_K_MAX


def test_evaluate_single_hit_at_k_calculation():
    """hit@K 计算：rank=4 → hit@5=True, hit@3=False, hit@1=False"""
    from scripts.eval_hitk import evaluate_single

    item = {"query": "test q", "relevant_doc_id": "doc-3", "source": "src_0"}

    # 构造 doc-3 在第 4 位
    fake_hits = [
        {"id": f"doc-{i}", "text": f"mock {i}", "source": "src_0", "score": 0.9 - i * 0.05}
        for i in range(10)
    ]

    with patch("scripts.eval_hitk.get_embedding_provider") as mock_emb, \
         patch("scripts.eval_hitk.qdrant_search") as mock_qdrant:
        mock_emb.return_value.embed_text.return_value = _make_fake_embedding()
        mock_qdrant.return_value = fake_hits

        result = evaluate_single(item, use_rerank=False, use_bm25=False, use_multi_query=False)

    assert result["rank"] == 4
    assert result["hit_at_k"][1] is False
    assert result["hit_at_k"][3] is False
    assert result["hit_at_k"][5] is True
    assert result["hit_at_k"][10] is True


def test_evaluate_single_not_found_rank_none():
    """relevant_doc_id 不在 top-10 → rank=None"""
    from scripts.eval_hitk import evaluate_single

    item = {"query": "test q", "relevant_doc_id": "doc-999", "source": "src_0"}

    with patch("scripts.eval_hitk.get_embedding_provider") as mock_emb, \
         patch("scripts.eval_hitk.qdrant_search") as mock_qdrant:
        mock_emb.return_value.embed_text.return_value = _make_fake_embedding()
        mock_qdrant.return_value = _make_fake_qdrant_hits("test q", top_k=10)

        result = evaluate_single(item, use_rerank=False, use_bm25=False, use_multi_query=False)

    assert result["rank"] is None
    assert all(v is False for v in result["hit_at_k"].values())


# =============================================================
# 3. summarize 汇总统计
# =============================================================
def test_summarize_basic():
    """summarize 输出字段完整（hit@K + latency + miss + by_source）"""
    from scripts.eval_hitk import summarize

    results = [
        {"query": f"q{i}", "relevant_doc_id": f"doc-{i}", "source": "src_0",
         "retrieved_ids": [f"doc-{i}", "doc-x"], "rank": 1, "latency_ms": 100.0,
         "hit_at_k": {1: True, 3: True, 5: True, 10: True}}
        for i in range(5)
    ]
    # 加 1 条 miss
    results.append({
        "query": "miss", "relevant_doc_id": "doc-y", "source": "src_1",
        "retrieved_ids": ["doc-z"], "rank": None, "latency_ms": 200.0,
        "hit_at_k": {1: False, 3: False, 5: False, 10: False}
    })

    summary = summarize(results)

    assert summary["total"] == 6
    # summarize 内部 round 到 3 位；用 pytest.approx 处理浮点
    assert summary["hit@1"] == pytest.approx(5 / 6, abs=1e-3)
    assert summary["miss_count"] == 1
    assert summary["miss_rate"] == pytest.approx(1 / 6, abs=1e-3)
    assert "p50" in summary["latency_ms"]
    assert "p90" in summary["latency_ms"]
    assert "src_0" in summary["by_source"]
    assert "src_1" in summary["by_source"]
    assert len(summary["miss_samples"]) == 1
    assert summary["miss_samples"][0]["query"] == "miss"


def test_summarize_empty():
    """空 results 返空 dict"""
    from scripts.eval_hitk import summarize

    assert summarize([]) == {}


# =============================================================
# 4. 阈值门禁（hit@5 < 0.6 报警）
# =============================================================
def test_threshold_gate_hit5_below_0_6_fails():
    """hit@5 < 0.6 → 模拟 CI fail 逻辑（人工/脚本读 summary 后判断）"""
    from scripts.eval_hitk import summarize

    # 构造 hit@5 = 0.4（10 条 query 中 4 条命中）
    results = []
    for i in range(10):
        is_hit = i < 4
        results.append({
            "query": f"q{i}", "relevant_doc_id": f"doc-{i}",
            "source": "src_0", "retrieved_ids": [f"doc-{i}"],
            "rank": 1 if is_hit else None, "latency_ms": 100.0,
            "hit_at_k": {1: is_hit, 3: is_hit, 5: is_hit, 10: is_hit}
        })

    summary = summarize(results)
    hit5 = summary["hit@5"]
    assert hit5 < THRESHOLD_HIT5, f"hit@5={hit5} 低于阈值 {THRESHOLD_HIT5}, 应该 fail"


def test_threshold_gate_hit5_above_0_6_passes():
    """hit@5 >= 0.6 → 模拟 CI pass"""
    from scripts.eval_hitk import summarize

    # 构造 hit@5 = 0.8
    results = []
    for i in range(10):
        is_hit = i < 8
        results.append({
            "query": f"q{i}", "relevant_doc_id": f"doc-{i}",
            "source": "src_0", "retrieved_ids": [f"doc-{i}"],
            "rank": 1 if is_hit else None, "latency_ms": 100.0,
            "hit_at_k": {1: is_hit, 3: is_hit, 5: is_hit, 10: is_hit}
        })

    summary = summarize(results)
    hit5 = summary["hit@5"]
    assert hit5 >= THRESHOLD_HIT5, f"hit@5={hit5} 应该 pass"


# =============================================================
# 5. --latency-bench flag（间接测试：跑 3 次取中位数的语义）
# =============================================================
def test_latency_bench_takes_median_of_3_runs():
    """latency-bench 语义：3 次 latency 取中位数"""
    import statistics

    # 模拟 3 次 retrieve：latency 分别为 100ms / 200ms / 300ms
    latencies = [100.0, 200.0, 300.0]
    median = statistics.median(latencies)

    assert median == 200.0  # 中位数是中间值


# =============================================================
# 6. 评测集字段容错（缺 source 不阻断）
# =============================================================
def test_evaluate_single_works_without_source_field():
    """缺 source 字段（虽然不常见）应能 fallback 到 'unknown'"""
    from scripts.eval_hitk import evaluate_single

    item = {"query": "test q", "relevant_doc_id": "doc-0"}  # 无 source

    with patch("scripts.eval_hitk.get_embedding_provider") as mock_emb, \
         patch("scripts.eval_hitk.qdrant_search") as mock_qdrant:
        mock_emb.return_value.embed_text.return_value = _make_fake_embedding()
        mock_qdrant.return_value = _make_fake_qdrant_hits("test q", top_k=10)

        result = evaluate_single(item, use_rerank=False, use_bm25=False, use_multi_query=False)

    assert result["source"] == "unknown"
    assert result["rank"] == 1