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
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 强制 A5 per-query 路径
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
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


# =============================================================
# 并行实测
# =============================================================
def test_parallel_3_queries_faster_than_serial():
    """并行：3 路 ≈ 0.3s（thread 启动开销 + CI 抖动容许到 0.6s）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 3
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 强制 A5 per-query 路径
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
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


def test_parallel_speedup_ratio():
    """加速比：串行/并行 ≥ SPEEDUP_MIN_RATIO（理论 3x，CI 留余量 2x）。"""
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    # 强制 A5 per-query 路径
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False

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
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


def test_parallel_5q_3w_batches():
    """5 路 / 3 workers：预期 ≈ 2 批 × 0.3s = 0.6s（容许 0.55-1.2s）。

    验证 max_workers 限制生效：5 个 query 不能同时跑，最多 3 个一批。
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService

    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 3
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False  # 强制 A5 per-query 路径
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
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True


# =============================================================
# Phase 4 A8: 融合后 rerank 验证
# =============================================================
# A8 rerank 慢函数：模拟 LLM rerank 调用 ~0.8s
RERANK_SLEEP_SECONDS = 0.8
# A5 总耗时下限（串行 + 每路 rerank）：3 × (0.3 + 0.8) = 3.3s
A5_SERIAL_MIN_SECONDS = 3.0
# A8 总耗时上限（并行 + 1×rerank）：0.3s 粗排 + 0.8s rerank ≈ 1.3s（容许 1.5s）
A8_PARALLEL_MAX_SECONDS = 1.5
# 加速比下限：A5_serial / A8_parallel ≥ 2.0x
A8_SPEEDUP_MIN_RATIO = 2.0


def _slow_full_search(query, top_k=3):
    """模拟 search_policy 完整路径：粗排 0.3s + rerank 0.8s = 1.1s"""
    time.sleep(SLEEP_PER_QUERY)
    time.sleep(RERANK_SLEEP_SECONDS)
    return _make_fake_hits(query, top_k)


def _slow_coarse(query, top_k=15):
    """模拟 search_policy_coarse：仅粗排 0.3s（无 rerank）"""
    time.sleep(SLEEP_PER_QUERY)
    return _make_fake_hits(query, 2)


def _slow_rerank(query, candidates, top_n=None):
    """模拟 QwenRerankProvider.rerank：sleep 0.8s + 加 rerank_score"""
    time.sleep(RERANK_SLEEP_SECONDS)
    result = []
    for i, c in enumerate(candidates[:top_n] if top_n else candidates):
        cc = dict(c)
        cc["rerank_score"] = 10 - i
        result.append(cc)
    return result


def test_a8_rerank_called_once_vs_a5_three_times():
    """A8 核心收益：rerank LLM 调用次数 1 vs 3（token 成本 -66%）。

    验证关键 KPI（与 wall clock 无关）：
    - A5（per-query rerank）：3 路 → 3 次 rerank LLM 调用
    - A8（fuse-first rerank）：3 路 → 1 次 rerank LLM 调用
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService
    from app.core.providers.rerank import get_rerank_provider

    # 准备一个统一的 rerank 计数器
    a5_calls = [0]
    a8_calls = [0]

    def counting_slow_search(query, top_k=3):
        time.sleep(SLEEP_PER_QUERY)
        time.sleep(RERANK_SLEEP_SECONDS)
        a5_calls[0] += 1
        return _make_fake_hits(query, top_k)

    def counting_slow_coarse(query, top_k=15):
        time.sleep(SLEEP_PER_QUERY)
        return _make_fake_hits(query, 2)

    def counting_slow_rerank(query, candidates, top_n=None):
        time.sleep(RERANK_SLEEP_SECONDS)
        a8_calls[0] += 1
        result = []
        for i, c in enumerate(candidates[:top_n] if top_n else candidates):
            cc = dict(c)
            cc["rerank_score"] = 10 - i
            result.append(cc)
        return result

    # A5 基线：fuse_first=False + parallel=True，每路 search_policy 含 rerank
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False
    settings.MULTI_QUERY_PARALLEL = True
    settings.MULTI_QUERY_WORKERS = 3
    a5_calls[0] = 0
    try:
        with patch.object(PolicyService, "search_policy", side_effect=counting_slow_search):
            PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        # A5：3 路 → 3 次 rerank
        assert a5_calls[0] == 3, \
            f"A5 应调 3 次 rerank，实际 {a5_calls[0]} 次"

        # A8：fuse_first=True + parallel=True，1 次融合后 rerank
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        a8_calls[0] = 0
        with patch.object(PolicyService, "search_policy_coarse", side_effect=counting_slow_coarse), \
             patch.object(get_rerank_provider(), "rerank", side_effect=counting_slow_rerank):
            PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)

        # A8：3 路 → 1 次 rerank
        assert a8_calls[0] == 1, \
            f"A8 应只调 1 次 rerank，实际 {a8_calls[0]} 次"

        # KPI 断言：rerank 调用次数 -66%
        reduction_pct = (a5_calls[0] - a8_calls[0]) / a5_calls[0] * 100
        assert reduction_pct >= 66, \
            f"A8 rerank 节省 {reduction_pct:.0f}%，应 ≥ 66%（a5={a5_calls[0]} a8={a8_calls[0]}）"
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        settings.MULTI_QUERY_PARALLEL = True
        settings.MULTI_QUERY_WORKERS = 3


def test_a8_parallel_faster_than_a5_serial():
    """A8 wall-clock 优势：A8 parallel vs A5 serial（最坏 vs 最好）。

    - A5 SERIAL：3 × (粗排 0.3s + rerank 0.8s) = 3.3s
    - A8 PARALLEL：3 路并行粗排 0.3s + 1 次 rerank 0.8s = 1.1s
    - 加速比 ≥ 2x

    这是 fuse-first + parallel 双重优化的最佳组合。
    """
    from app.core.config import settings
    from app.services.policy_service import PolicyService
    from app.core.providers.rerank import get_rerank_provider

    # A5 SERIAL：fuse_first=False + parallel=False（最坏场景）
    settings.MULTI_QUERY_FUSE_FIRST_RERANK = False
    settings.MULTI_QUERY_PARALLEL = False
    try:
        with patch.object(PolicyService, "search_policy", side_effect=_slow_full_search):
            start = time.perf_counter()
            PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)
            t_a5_serial = time.perf_counter() - start

        assert t_a5_serial >= A5_SERIAL_MIN_SECONDS, (
            f"A5 serial baseline 耗时 {t_a5_serial:.3f}s 不足 {A5_SERIAL_MIN_SECONDS}s "
            f"（说明 search_policy 没真跑 coarse+rerank）"
        )

        # A8 PARALLEL：fuse_first=True + parallel=True（最好场景）
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
        settings.MULTI_QUERY_PARALLEL = True
        settings.MULTI_QUERY_WORKERS = 3
        with patch.object(PolicyService, "search_policy_coarse", side_effect=_slow_coarse), \
             patch.object(get_rerank_provider(), "rerank", side_effect=_slow_rerank):
            start = time.perf_counter()
            PolicyService.search_multi_policy(["q1", "q2", "q3"], top_k=3)
            t_a8_parallel = time.perf_counter() - start

        speedup = t_a5_serial / t_a8_parallel
        assert t_a8_parallel <= A8_PARALLEL_MAX_SECONDS, (
            f"A8 parallel 耗时 {t_a8_parallel:.3f}s 超出上限 {A8_PARALLEL_MAX_SECONDS}s "
            f"（预期 ≈ 1.1s：0.3s 粗排 + 0.8s 单次 rerank）"
        )
        assert speedup >= A8_SPEEDUP_MIN_RATIO, (
            f"A8 parallel vs A5 serial 加速比 {speedup:.2f}x 不足 {A8_SPEEDUP_MIN_RATIO}x "
            f"（a5_serial={t_a5_serial:.3f}s a8_parallel={t_a8_parallel:.3f}s）"
        )
    finally:
        settings.MULTI_QUERY_FUSE_FIRST_RERANK = True
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
    test_a8_rerank_called_once_vs_a5_three_times()
    print("[OK] A8 rerank called 1x vs A5 3x (token cost -66%)")
    test_a8_parallel_faster_than_a5_serial()
    print("[OK] A8 parallel > A5 serial (wall clock 3x speedup)")
    print("\nALL LATENCY BENCHMARKS PASSED")