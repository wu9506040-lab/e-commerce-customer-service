"""
test_bm25_eager.py — P1-2 L1 单测：BM25 索引后台异步重建

按 SOP-V1 §2.2 数据可信验证：L1 mock 验证逻辑路径；
L2 集成测试（test_bm25_eager_integration.py）用真 Qdrant 验证 rebuild 真生效。

测试目标：
- invalidate_and_rebuild_async() 启动守护线程，不阻塞主流程
- 线程内 _build_index 成功 → 写回全局 _INDEX
- 线程内异常 → 不抛（fire-and-forget + log）
- ingest_text 触发逻辑：开关 + qdrant_written > 0 双条件
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.services import bm25_index
from app.services.bm25_index import invalidate_and_rebuild_async
from app.services.rag import ingest


@pytest.fixture(autouse=True)
def reset_bm25_index():
    """autouse fixture：每个测试前 reset _INDEX，避免污染下游

    按 feedback_test_factory_singleton 经验：fire-and-forget 测试如 reload/patch 全局
    必须 try/finally + saved_globals 备份恢复，否则污染下游测试。
    """
    saved_index = bm25_index._INDEX
    bm25_index._INDEX = None
    try:
        yield
    finally:
        bm25_index._INDEX = saved_index


# =============================================================
# Case 1: invalidate_and_rebuild_async() 启动守护线程
# =============================================================
def test_invalidate_and_rebuild_async_starts_thread():
    """调用后立即启动守护线程，不阻塞主流程（耗时 < 100ms）

    关键：用 threading.Event 卡住 mock 的 _build_index，确保线程在测试断言时
    还没写回 _INDEX（验证"立即返回不等待"语义）。
    """
    build_started = threading.Event()

    def slow_mock_build():
        build_started.set()
        # 阻塞直到测试结束（模拟真实 Qdrant scroll 耗时 1-3s）
        time.sleep(2.0)
        return {"docs": [], "bm25": None, "doc_id_map": {}}

    with patch.object(bm25_index, "_build_index", side_effect=slow_mock_build):
        start = time.monotonic()
        bm25_index.invalidate_and_rebuild_async()
        elapsed = time.monotonic() - start

        # 断言：主流程返回时线程已启动但未完成（slow_mock 阻塞中）
        assert build_started.wait(timeout=0.5), "后台线程未在 500ms 内启动"
        # 主流程应 < 100ms（仅启动线程 + invalidate，不等 rebuild）
        assert elapsed < 0.1
        # invalidate 立即生效（_INDEX 已被置 None，且 rebuild 阻塞中尚未写回）
        assert bm25_index._INDEX is None


# =============================================================
# Case 2: 后台线程成功重建 → 写回 _INDEX
# =============================================================
def test_rebuild_thread_writes_back_to_index():
    """后台线程调 _build_index，成功后用 LOCK 写回 _INDEX

    验证方式：join 线程（用短 timeout）确保重建完成，再读 _INDEX
    """
    fake_index = {"docs": [{"id": "1", "text": "fake"}], "bm25": MagicMock(), "doc_id_map": {"1": 0}}

    with patch.object(bm25_index, "_build_index", return_value=fake_index):
        bm25_index.invalidate_and_rebuild_async()
        # 给守护线程 ~500ms 完成（mock 是同步 return，应该 < 100ms）
        time.sleep(0.5)

    # 后台线程已写回 _INDEX
    assert bm25_index._INDEX is not None
    assert bm25_index._INDEX["docs"] == fake_index["docs"]


# =============================================================
# Case 3: 后台线程异常 → 不抛（fire-and-forget）
# =============================================================
def test_rebuild_thread_exception_does_not_propagate(caplog):
    """线程内 _build_index 抛异常 → 不影响主流程（log warning + 兜底）"""
    with patch.object(bm25_index, "_build_index", side_effect=RuntimeError("fake qdrant error")):
        # 不应抛
        bm25_index.invalidate_and_rebuild_async()
        time.sleep(0.5)  # 等线程失败

    # 主流程正常返回（调用方无感知）
    # log 应有失败记录（caplog 验证）
    assert any("BM25 索引后台重建失败" in r.message for r in caplog.records)


# =============================================================
# Case 4: 守护线程属性验证（daemon=True）
# =============================================================
def test_rebuild_thread_is_daemon():
    """后台线程是守护线程（daemon=True），主进程退出时不等待"""
    # 在测试中捕获启动的线程对象
    started_threads = []
    real_thread_init = threading.Thread.__init__

    def spy_init(self, *args, **kwargs):
        started_threads.append(self)
        real_thread_init(self, *args, **kwargs)

    with patch.object(bm25_index, "_build_index", return_value={"docs": [], "bm25": None, "doc_id_map": {}}), \
         patch.object(threading.Thread, "__init__", spy_init):
        bm25_index.invalidate_and_rebuild_async()

    assert len(started_threads) == 1
    assert started_threads[0].daemon is True
    assert started_threads[0].name == "bm25-rebuild"


# =============================================================
# Case 5: ingest_text 触发 rebuild · 开关 ON + qdrant_written > 0
# =============================================================
def test_ingest_text_triggers_rebuild_when_enabled_and_qdrant_written(monkeypatch):
    """RAG_BM25_EAGER_BUILD=True + qdrant_written=3 → 触发 invalidate_and_rebuild_async"""
    monkeypatch.setattr(ingest.settings, "RAG_BM25_EAGER_BUILD", True)

    called = {"count": 0}

    def fake_rebuild():
        called["count"] += 1

    with patch.object(ingest, "ensure_collection", return_value=True), \
         patch.object(ingest, "get_embedding_provider") as mock_embed, \
         patch.object(ingest, "upsert_points", return_value=3), \
         patch.object(ingest, "upsert_knowledge_meta", return_value=MagicMock(id=1)), \
         patch("app.services.bm25_index.invalidate_and_rebuild_async", fake_rebuild):
        ingest.ingest_text("测试文本" * 100, source="trigger_test")

    assert called["count"] == 1  # ✅ 触发 1 次


# =============================================================
# Case 6: 开关 OFF → 不触发
# =============================================================
def test_ingest_text_does_not_trigger_rebuild_when_disabled(monkeypatch):
    """RAG_BM25_EAGER_BUILD=False → 不触发 rebuild（懒加载兜底）"""
    monkeypatch.setattr(ingest.settings, "RAG_BM25_EAGER_BUILD", False)

    called = {"count": 0}

    def fake_rebuild():
        called["count"] += 1

    with patch.object(ingest, "ensure_collection", return_value=True), \
         patch.object(ingest, "get_embedding_provider"), \
         patch.object(ingest, "upsert_points", return_value=3), \
         patch.object(ingest, "upsert_knowledge_meta", return_value=MagicMock(id=1)), \
         patch("app.services.bm25_index.invalidate_and_rebuild_async", fake_rebuild):
        ingest.ingest_text("测试文本" * 100, source="disabled_test")

    assert called["count"] == 0  # ✅ 不触发


# =============================================================
# Case 7: qdrant_written == 0 → 不触发（断路器开路场景）
# =============================================================
def test_ingest_text_does_not_trigger_rebuild_when_qdrant_zero(monkeypatch):
    """upsert_points 返 0（断路器开路）→ 无须 rebuild（Qdrant 没新数据）"""
    monkeypatch.setattr(ingest.settings, "RAG_BM25_EAGER_BUILD", True)

    called = {"count": 0}

    def fake_rebuild():
        called["count"] += 1

    with patch.object(ingest, "ensure_collection", return_value=True), \
         patch.object(ingest, "get_embedding_provider"), \
         patch.object(ingest, "upsert_points", return_value=0), \
         patch.object(ingest, "upsert_knowledge_meta", return_value=MagicMock(id=1)), \
         patch("app.services.bm25_index.invalidate_and_rebuild_async", fake_rebuild):
        ingest.ingest_text("测试文本" * 100, source="qdrant_zero_test")

    assert called["count"] == 0  # ✅ qdrant_written=0 不触发


# =============================================================
# Case 8: rebuild 启动失败 → 不抛（fire-and-forget）
# =============================================================
def test_ingest_text_handles_rebuild_trigger_failure(monkeypatch):
    """invalidate_and_rebuild_async 抛异常 → ingest_text 正常返（不掩盖主流程）"""
    monkeypatch.setattr(ingest.settings, "RAG_BM25_EAGER_BUILD", True)

    def broken_rebuild():
        raise RuntimeError("fake thread start failure")

    with patch.object(ingest, "ensure_collection", return_value=True), \
         patch.object(ingest, "get_embedding_provider"), \
         patch.object(ingest, "upsert_points", return_value=3), \
         patch.object(ingest, "upsert_knowledge_meta", return_value=MagicMock(id=1)), \
         patch("app.services.bm25_index.invalidate_and_rebuild_async", broken_rebuild):
        # 不应抛
        result = ingest.ingest_text("测试文本" * 100, source="trigger_fail_test")

    # 主流程正常
    assert result["ingested_chunks"] >= 1