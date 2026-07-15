"""
Phase 4 A5: Multi-Query 并行检索 Latency Benchmark

目的：用 mock 慢函数模拟 search_policy 的真实 IO 延迟（rerank LLM ~800ms），
     实测串行 vs 并行的加速比，验证 ThreadPoolExecutor 并行改造确实生效。

策略：
- fake search_policy = time.sleep(SLEEP_PER_QUERY) + 返回 hits
- SLEEP_PER_QUERY = 0.3s（覆盖 embedding + qdrant + rerank 综合延迟）
- 串行基线 3 路 ≈ 0.9s；并行 3 路 ≈ 0.3s；预期加速比 ≥ 2x

注意：
- 这是**逻辑正确性 + 加速比下限**测试，不是性能保证
- 容许范围宽（thread 启动 + CI 抖动 + GIL 调度）
- 不替代真实负载压测（应跑 dev/ECS 才能拿到真实数字）

如果 CI 长期 flaky：
- 放宽 [0.20, 0.8]（并行）/ [0.80, 1.5]（串行）
- 或加 @pytest.mark.slow 标记跳过
"""
import os
import sys
import time
from unittest.mock import patch

# pytest 不走 __main__，env 必须在模块顶部 setdefault
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================
# 配置
# =============================================================
# 单路模拟延迟（覆盖 embedding + qdrant + rerank 综合）
SLEEP_PER_QUERY = 0.3
# 并行模式耗时上限（thread 启动开销 + CI 抖动留余量）
PARALLEL_MAX_SECONDS = 0.6
# 串行基线耗时上限（CI 慢环境下放宽到 1.5s）
SERIAL_MAX_SECONDS = 1.2
# 串行基线下限（3 路 × 0.3s = 0.9s）
SERIAL_MIN_SECONDS = 0.85
# 加速比下限（理论 3x，CI 抖动留余量到 2x）
SPEEDUP_MIN_RATIO = 2.0


# =============================================================
# 工具函数
# =============================================================
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


def _slow_search(query, top_k=3):
    """模拟 search_policy：sleep SLEEP_PER_QUERY + 返 fake hits"""
    time.sleep(SLEEP_PER_QUERY)
    return _make_fake_hits(query, 2)


# =============================================================
# 串行基线
# =============================================================
def test_serial_3_queries_baseline():
    """串行：3 路 × 0.3s ≈ 0.9s（CI 抖动容许 0.85-1.2s）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = False
    try:
        with patch.object(PolicyService, "search_policy", side_effect=_slow_search):
            start = time.perf_counter()
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)
            elapsed = time.perf_counter() - start

        assert len(result) > 0
        assert SERIAL_MIN_SECONDS <= elapsed <= SERIAL_MAX_SECONDS, (
            f"串行 3 路耗时 {elapsed:.3f}s 不在预期 "
            f"[{SERIAL_MIN_SECONDS}, {SERIAL_MAX_SECONDS}]"
        )
    finally:
        settings.MULTI_QUERY_PARALLEL = True


# =============================================================
# 并行实测
# =============================================================
def test_parallel_3_queries_faster_than_serial():
    """并行：3 路 ≈ 0.3s（thread 启动开销 + CI 抖动容许到 0.6s）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 3
    try:
        with patch.object(PolicyService, "search_policy", side_effect=_slow_search):
            start = time.perf_counter()
            result = PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)
            elapsed = time.perf_counter() - start

        assert len(result) > 0
        assert 0.20 <= elapsed <= PARALLEL_MAX_SECONDS, (
            f"并行 3 路耗时 {elapsed:.3f}s 不在预期 "
            f"[0.20, {PARALLEL_MAX_SECONDS}]"
        )
    finally:
        settings.MULTI_QUERY_PARALLEL = True
        settings.MULTI_QUERY_WORKERS = 3


def test_parallel_speedup_ratio():
    """加速比：串行/并行 ≥ SPEEDUP_MIN_RATIO（理论 3x，CI 留余量 2x）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    # 串行基线
    settings.MULTI_QUERY_PARALLEL = False
    with patch.object(PolicyService, "search_policy", side_effect=_slow_search):
        start = time.perf_counter()
        PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)
        t_serial = time.perf_counter() - start

    # 并行实测
    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 3
    try:
        with patch.object(PolicyService, "search_policy", side_effect=_slow_search):
            start = time.perf_counter()
            PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)
            t_parallel = time.perf_counter() - start

        speedup = t_serial / t_parallel
        assert speedup >= SPEEDUP_MIN_RATIO, (
            f"加速比 {speedup:.2f}x 不足 {SPEEDUP_MIN_RATIO}x "
            f"（serial={t_serial:.3f}s parallel={t_parallel:.3f}s）"
        )
    finally:
        settings.MULTI_QUERY_PARALLEL = True
        settings.MULTI_QUERY_WORKERS = 3


def test_parallel_5q_3w_batches():
    """5 路 / 3 workers：预期 ≈ 2 批 × 0.3s = 0.6s（容许 0.55-1.2s）。

    验证 max_workers 限制生效：5 个 query 不能同时跑，最多 3 个一批。
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 3
    try:
        queries = [f"q{i}" for i in range(5)]
        with patch.object(PolicyService, "search_policy", side_effect=_slow_search):
            start = time.perf_counter()
            result = PolicyService.search_multi_policy(queries, top_k=3)
            elapsed = time.perf_counter() - start

        assert len(result) > 0
        # 5 路 / 3 workers → ceil(5/3) = 2 批 → ~ 0.6s
        # 容许上限放宽到 1.2s（批间调度 + CI 抖动）
        assert 0.55 <= elapsed <= 1.2, (
            f"5 路 / 3 workers 耗时 {elapsed:.3f}s 不在预期 [0.55, 1.2]"
        )
    finally:
        settings.MULTI_QUERY_PARALLEL = True
        settings.MULTI_QUERY_WORKERS = 3


if __name__ == "__main__":
    test_serial_3_queries_baseline()
    print("[OK] serial 3x baseline")
    test_parallel_3_queries_faster_than_serial()
    print("[OK] parallel 3x faster")
    test_parallel_speedup_ratio()
    print("[OK] speedup ratio >= 2x")
    test_parallel_5q_3w_batches()
    print("[OK] 5 queries / 3 workers batches")
    print("\nALL LATENCY BENCHMARKS PASSED")