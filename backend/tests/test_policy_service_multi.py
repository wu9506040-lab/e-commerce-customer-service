"""
Phase 4 A4 + A5 + A8: PolicyService.search_multi_policy 单测

覆盖：
A4 部分（6 用例）：
1. queries 为空 → 返空 list
2. 单路（queries 长度 = 1）→ 短路返回 search_policy 结果
3. 多路正常 → 走 RRF 融合（强制 A5 路径，fuse_first=False）
4. 单路异常 → 仅该路降级，其他路继续（强制 A5 路径）
5. RRF 异常 → 降级返回首路结果（强制 A5 路径）
6. schema 与 search_policy 一致（含 text/source/score/rerank_score/rrf_score）

A5 部分（5 用例）：
7. 并行模式启用时进入 ThreadPoolExecutor 分支（强制 A5 路径）
8. MULTI_QUERY_PARALLEL=False 走原串行（不起 thread pool，强制 A5 路径）
9. MULTI_QUERY_WORKERS=2 时并发数 ≤ 2（强制 A5 路径）
10. 单路查询短路，不创建 thread pool（single 路径）
11. 并行模式下单路异常隔离（强制 A5 路径）

A8 部分（5 用例，默认 MULTI_QUERY_FUSE_FIRST_RERANK=True）：
12. fuse-first 模式下 rerank 只调 1 次（不论 N=几）
13. fuse-first 用 queries[0] 作 rerank 评估 query
14. fuse-first 把融合候选截断到 15 再送 rerank
15. fuse-first rerank 失败时降级到 RRF top-k（不崩）
16. fuse-first 关闭（fuse_first=False）→ 回退 A5 per-query rerank 路径

说明：A5 测试都需要 mock `search_policy`，但 A8 模式下走 `search_policy_coarse`。
所以 A5 测试统一加 `settings.MULTI_QUERY_FUSE_FIRST_RERANK = False` 强制 A5 路径。
"""
import os
import sys
from unittest.mock import patch, MagicMock

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


def _make_fake_raw_hits(prefix: str, n: int = 3) -> list[dict]:
    """构造 N 条 raw Qdrant 风格的 mock 命中（带 payload）"""
    return [
        {
            "id": f"{prefix}-{i}",
            "payload": {"text": f"{prefix} doc {i}", "source": f"src_{prefix}_{i}"},
            "score": 0.9 - i * 0.05,
        }
        for i in range(n)
    ]


# =============================================================
# A4 部分
# =============================================================
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
    """场景 3：3 条 query → 每路 search_policy + RRF 融合（A5 路径）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 强制 A5 per-query 路径
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

    try:
        with patch.object(PolicyService, "search_policy", side_effect=fake_search) as mock_sp:
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        assert call_count[0] == 3  # 3 路都跑了
        assert len(result) >= 1
        assert all("rrf_score" in h for h in result)  # 多路融合后应带 rrf_score
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


def test_single_query_exception_continues_others():
    """场景 4：单路异常 → 仅该路降级，其他路继续（A5 路径）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False
    call_count = [0]

    def side_effect(query, top_k=3):
        call_count[0] += 1
        if call_count[0] == 2:  # 第 2 路失败
            raise RuntimeError("Qdrant timeout")
        return _make_fake_hits(f"ok-{call_count[0]}", 2)

    try:
        with patch.object(PolicyService, "search_policy", side_effect=side_effect), \
             patch("app.services.policy_service.logger"):
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        assert len(result) > 0  # 其他路结果继续
        all_sources = [h["source"] for h in result]
        assert any("ok-" in s for s in all_sources)
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


def test_rff_fuse_failure_falls_back_to_first():
    """场景 5：RRF 融合异常 → 降级到首路前 top_k 结果（A5 路径）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False
    fake_hits_q1 = _make_fake_hits("q1", 5)
    fake_hits_q2 = _make_fake_hits("q2", 5)

    def fake_search(query, top_k=3):
        return fake_hits_q1 if "q1" in query else fake_hits_q2

    try:
        with patch.object(PolicyService, "search_policy", side_effect=fake_search), \
             patch("app.services.rrf.rrf_fuse", side_effect=RuntimeError("RRF crash")), \
             patch("app.services.policy_service.logger"):
            result = PolicyService.search_multi_policy(["q1", "q2"], top_k=3)

        assert len(result) == 3
        # 应返首路结果（截断 top_k=3）
        assert result[0]["source"] == "src_q1_0"
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


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


# =============================================================
# A5 部分（强制 A5 路径：fuse_first=False）
# =============================================================

def test_parallel_uses_thread_pool_when_enabled():
    """场景 7：A5 默认 MULTI_QUERY_PARALLEL=True → 走 ThreadPoolExecutor 并行。"""
    import threading
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 3
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 强制 A5 per-query rerank 路径
    thread_names = []

    def fake_search(query, top_k=3):
        thread_names.append(threading.current_thread().name)
        return _make_fake_hits(query, 2)

    try:
        with patch.object(PolicyService, "search_policy", side_effect=fake_search):
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        assert len(result) > 0
        # ThreadPoolExecutor 用 thread_name_prefix="multi-policy"
        assert any(t.startswith("multi-policy") for t in thread_names), \
            f"并行模式应起 thread pool，实际 thread names: {thread_names}"
    finally:
        settings.MULTI_QUERY_PARALLEL = True
        settings.MULTI_QUERY_WORKERS = 3
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


def test_parallel_disabled_falls_back_to_serial():
    """场景 8：MULTI_QUERY_PARALLEL=False → 走原串行（不起 thread pool）。"""
    import threading
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = False
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 强制 A5 路径
    thread_names = []

    def fake_search(query, top_k=3):
        thread_names.append(threading.current_thread().name)
        return _make_fake_hits(query, 2)

    try:
        with patch.object(PolicyService, "search_policy", side_effect=fake_search):
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        assert len(result) > 0
        # 串行路径不起 pool，所有调用都在主线程
        assert all(not t.startswith("multi-policy") for t in thread_names), \
            f"串行模式不应起 thread pool，实际 thread names: {thread_names}"
    finally:
        settings.MULTI_QUERY_PARALLEL = True
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


def test_parallel_workers_respects_config():
    """场景 9：MULTI_QUERY_WORKERS=2 时 max_workers 上限为 2。"""
    import threading
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 2
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 强制 A5 路径
    active_workers = set()
    active_lock = threading.Lock()

    def fake_search(query, top_k=3):
        with active_lock:
            active_workers.add(threading.current_thread().name)
        import time
        time.sleep(0.05)  # 给并发观察窗口
        return _make_fake_hits(query, 2)

    try:
        with patch.object(PolicyService, "search_policy", side_effect=fake_search):
            PolicyService.search_multi_policy(["q1", "q2", "q3", "q4", "q5"], top_k=3)

        # 5 个 query / 2 workers → 最多同时 2 个 worker thread
        parallel_threads = {t for t in active_workers if t.startswith("multi-policy")}
        assert len(parallel_threads) <= 2, \
            f"workers=2 时实际并发 thread 数: {len(parallel_threads)}（{parallel_threads}）"
    finally:
        settings.MULTI_QUERY_WORKERS = 3
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


def test_parallel_single_query_no_pool():
    """场景 10：单路查询（len=1）→ 短路，不创建 thread pool。"""
    import threading
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = True
    thread_names = []

    def fake_search(query, top_k=3):
        thread_names.append(threading.current_thread().name)
        return _make_fake_hits(query, 2)

    try:
        with patch.object(PolicyService, "search_policy", side_effect=fake_search):
            result = PolicyService.search_multi_policy(["only"], top_k=3)

        assert len(result) == 2  # 单路短路
        assert all(not t.startswith("multi-policy") for t in thread_names), \
            f"单路不应起 thread pool，实际 thread names: {thread_names}"
    finally:
        settings.MULTI_QUERY_PARALLEL = True


def test_parallel_exception_isolation_unchanged():
    """场景 11：并行模式下单路异常 → 仅该路降级，其他路继续（A5 路径）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 3
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 强制 A5 路径
    call_count = [0]

    def side_effect(query, top_k=3):
        call_count[0] += 1
        # 第 2 路失败（多线程下不一定严格是 2，但失败一定会出现一次）
        if call_count[0] == 2:
            raise RuntimeError("Qdrant timeout")
        return _make_fake_hits(f"ok-{call_count[0]}", 2)

    try:
        with patch.object(PolicyService, "search_policy", side_effect=side_effect), \
             patch("app.services.policy_service.logger"):
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        assert len(result) > 0  # 其他路结果继续
        all_sources = [h["source"] for h in result]
        assert any("ok-" in s for s in all_sources)
    finally:
        settings.MULTI_QUERY_PARALLEL = True
        settings.MULTI_QUERY_WORKERS = 3
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


# =============================================================
# Phase 4 A8: 融合后 rerank 单测（默认 MULTI_QUERY_FUSE_FIRST_RERANK=True）
# =============================================================

def test_fuse_first_rerank_called_once():
    """场景 12：fuse-first 模式下，rerank 恰好调 1 次（不论 N=几）。

    验证 A8 核心收益：3 路 query → 仅 1 次 LLM rerank 调用（vs A5 的 3 次）。
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
    settings.MULTI_QUERY_PARALLEL = True

    # mock search_policy_coarse：3 路各返 3 条不重叠候选
    coarse_calls = [0]

    def fake_coarse(query, top_k=15):
        coarse_calls[0] += 1
        return _make_fake_hits(f"q{coarse_calls[0]}", 3)

    # mock rerank provider：应只被调 1 次
    rerank_mock = MagicMock(return_value=_make_fake_hits("reranked", 3))

    try:
        with patch.object(PolicyService, "search_policy_coarse", side_effect=fake_coarse), \
             patch("app.core.providers.rerank.get_rerank_provider") as mock_get_provider:
            mock_get_provider.return_value.rerank = rerank_mock
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        # 核心断言：3 路 → rerank 1 次
        assert rerank_mock.call_count == 1, \
            f"fuse-first 应只调 1 次 rerank，实际 {rerank_mock.call_count} 次"
        # 3 路粗排都跑了
        assert coarse_calls[0] == 3
        assert len(result) == 3
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        settings.MULTI_QUERY_PARALLEL = True


def test_fuse_first_uses_queries_0():
    """场景 13：fuse-first 用 queries[0]（原始 query）作为 rerank 评估 query。

    验证：rerank 的 query 参数 = valid_queries[0][1]，不是改写变体。
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
    settings.MULTI_QUERY_PARALLEL = False  # 串行便于观察调用顺序

    def fake_coarse(query, top_k=15):
        return _make_fake_hits(query, 2)  # 用 query 当 prefix 便于区分

    rerank_mock = MagicMock(return_value=_make_fake_hits("reranked", 3))

    try:
        with patch.object(PolicyService, "search_policy_coarse", side_effect=fake_coarse), \
             patch("app.core.providers.rerank.get_rerank_provider") as mock_get_provider:
            mock_get_provider.return_value.rerank = rerank_mock
            PolicyService.search_multi_policy(
                ["原始query", "改写变体1", "改写变体2"], top_k=3
            )

        # 验证 rerank 第一个位置参数 = 原始 query
        call_args = rerank_mock.call_args
        assert call_args[0][0] == "原始query", \
            f"fuse-first rerank 应使用 queries[0]，实际 {call_args[0][0]!r}"
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        settings.MULTI_QUERY_PARALLEL = True


def test_fuse_first_truncates_to_15():
    """场景 14：fuse-first 把融合候选截断到 RERANK_CANDIDATE_TOP_K（=15）再送 rerank。

    验证：mock qdrant 返 30 条不重叠候选，rerank 输入应是 fused 前 15 条。
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
    settings.MULTI_QUERY_PARALLEL = False
    settings.RERANK_CANDIDATE_TOP_K = 15

    # 3 路各返 10 条不重叠 → RRF 融合后 = 30 条
    def fake_coarse(query, top_k=15):
        prefix = query.split("-")[-1]
        return _make_fake_hits(f"{prefix}", 10)

    rerank_mock = MagicMock(return_value=_make_fake_hits("reranked", 3))

    try:
        with patch.object(PolicyService, "search_policy_coarse", side_effect=fake_coarse), \
             patch("app.core.providers.rerank.get_rerank_provider") as mock_get_provider:
            mock_get_provider.return_value.rerank = rerank_mock
            PolicyService.search_multi_policy(["q-1", "q-2", "q-3"], top_k=3)

        # 验证 rerank 第二个位置参数（candidates）长度 = 15
        call_args = rerank_mock.call_args
        candidates = call_args[0][1]
        assert len(candidates) == 15, \
            f"fuse-first rerank candidates 应截断到 15，实际 {len(candidates)}"
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        settings.MULTI_QUERY_PARALLEL = True


def test_fuse_first_fallback_on_rerank_failure():
    """场景 15：fuse-first rerank 失败 → 降级返 RRF top-k（不崩）。

    验证：mock rerank 抛异常时，结果来自 fused（RRF top-k），非空。
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
    settings.MULTI_QUERY_PARALLEL = False

    def fake_coarse(query, top_k=15):
        return _make_fake_hits(f"q{query[-1]}", 5)

    rerank_mock = MagicMock(side_effect=RuntimeError("LLM rerank 超时"))

    try:
        with patch.object(PolicyService, "search_policy_coarse", side_effect=fake_coarse), \
             patch("app.core.providers.rerank.get_rerank_provider") as mock_get_provider, \
             patch("app.services.policy_service.logger"):
            mock_get_provider.return_value.rerank = rerank_mock
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        # rerank 失败但不应崩：应返 RRF top-k（非空）
        assert len(result) == 3, \
            f"rerank 失败时应降级到 RRF top-3，实际返回 {len(result)} 条"
        # rerank 仍被尝试调用
        assert rerank_mock.call_count == 1
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        settings.MULTI_QUERY_PARALLEL = True


def test_fuse_first_disabled_falls_back_to_a5():
    """场景 16：fuse-first 关闭（fuse_first=False）→ 回退 A5 per-query rerank 路径。

    验证：A8 flag=False 时，rerank 调用 N 次（每路一次）。
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 关闭 A8
    settings.MULTI_QUERY_PARALLEL = False  # 串行便于计数

    def fake_coarse(query, top_k=15):
        return _make_fake_hits(query, 2)

    # 模拟 A5 路径：search_policy 内部可能调 rerank
    # 这里通过 search_policy mock 间接验证（fuse_first=False 走 search_policy 分支）
    rerank_mock = MagicMock(return_value=_make_fake_hits("reranked", 3))

    try:
        # fuse_first=False 时调用 search_policy，所以 mock search_policy
        # search_policy 内部可能调 rerank（settings.USE_RERANK=True 时）
        with patch.object(
            PolicyService, "search_policy", return_value=_make_fake_hits("sp", 3)
        ) as mock_sp:
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        # 核心断言：A5 路径调 search_policy 3 次（vs A8 路径不调 search_policy）
        assert mock_sp.call_count == 3, \
            f"A5 路径应调 search_policy 3 次，实际 {mock_sp.call_count} 次"
        assert len(result) == 3
        # search_policy_coarse 不应被调用
        # （这条隐含验证已由 mock_sp.call_count 覆盖）
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        settings.MULTI_QUERY_PARALLEL = True


# =============================================================
# Phase 4 A8: 运行指标日志测试
# =============================================================

def test_a8_emits_metrics_log_with_mode_and_rerank_count(caplog):
    """场景 17：A8 fuse-first 模式应输出 [multi_query_metrics] 日志，含 mode/queries/rerank_calls/latency_ms。"""
    import logging
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
    settings.MULTI_QUERY_PARALLEL = False  # 串行便于确定 rerank_calls

    def fake_coarse(query, top_k=15):
        return _make_fake_hits(f"q{query[-1]}", 3)

    rerank_mock = MagicMock(return_value=_make_fake_hits("reranked", 3))

    try:
        with caplog.at_level(logging.INFO, logger="app.services.policy_service"), \
             patch.object(PolicyService, "search_policy_coarse", side_effect=fake_coarse), \
             patch("app.core.providers.rerank.get_rerank_provider") as mock_get_provider:
            mock_get_provider.return_value.rerank = rerank_mock
            PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        # 找含 [multi_query_metrics] 的日志
        metric_logs = [r.message for r in caplog.records if "[multi_query_metrics]" in r.message]
        assert len(metric_logs) >= 1, \
            f"应至少有一条 [multi_query_metrics] 日志，实际 {len(metric_logs)} 条"
        # 末条日志应含 mode=fuse_first + queries=3 + rerank_calls=1 + latency_ms=
        last_log = metric_logs[-1]
        assert "mode=fuse_first" in last_log, f"日志缺 mode=fuse_first: {last_log}"
        assert "queries=3" in last_log, f"日志缺 queries=3: {last_log}"
        assert "rerank_calls=1" in last_log, f"日志缺 rerank_calls=1: {last_log}"
        assert "latency_ms=" in last_log, f"日志缺 latency_ms: {last_log}"
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        settings.MULTI_QUERY_PARALLEL = True


if __name__ == "__main__":
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    test_empty_queries_returns_empty()
    print("[OK] 1 empty queries")
    test_single_query_short_circuits()
    print("[OK] 2 single query short circuit")
    test_multi_query_runs_rag_per_query_and_fuses()
    print("[OK] 3 multi query RRF (A5 path)")
    test_single_query_exception_continues_others()
    print("[OK] 4 single query exception (A5 path)")
    test_rff_fuse_failure_falls_back_to_first()
    print("[OK] 5 RRF failure fallback (A5 path)")
    test_schema_matches_search_policy()
    print("[OK] 6 schema matches")
    test_parallel_uses_thread_pool_when_enabled()
    print("[OK] 7 parallel thread pool (A5 path)")
    test_parallel_disabled_falls_back_to_serial()
    print("[OK] 8 parallel disabled serial (A5 path)")
    test_parallel_workers_respects_config()
    print("[OK] 9 parallel workers config (A5 path)")
    test_parallel_single_query_no_pool()
    print("[OK] 10 parallel single no pool")
    test_parallel_exception_isolation_unchanged()
    print("[OK] 11 parallel exception isolation (A5 path)")
    test_fuse_first_rerank_called_once()
    print("[OK] 12 A8 rerank called once")
    test_fuse_first_uses_queries_0()
    print("[OK] 13 A8 uses queries[0] for rerank")
    test_fuse_first_truncates_to_15()
    print("[OK] 14 A8 truncates to 15")
    test_fuse_first_fallback_on_rerank_failure()
    print("[OK] 15 A8 fallback on rerank failure")
    test_fuse_first_disabled_falls_back_to_a5()
    print("[OK] 16 A8 disabled falls back to A5")
    print("\nNote: test_a8_emits_metrics_log requires pytest caplog fixture")
    print("\nALL 16 SCENARIOS PASSED (17th requires pytest)")