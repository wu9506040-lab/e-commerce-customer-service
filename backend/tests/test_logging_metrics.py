"""
test_logging_metrics.py - M8 结构化日志 + 指标测试

覆盖：
- ContextVar set/get/reset 隔离
- RequestIdMiddleware：生成 / 透传 / 响应头
- JSONFormatter 输出格式正确
- ContextFilter 注入上下文字段
- Metrics 计数器线程安全
- hit@K 计算
- snapshot() 导出完整字段
"""
import asyncio
import io
import json
import logging
import os
import threading
import time
from unittest.mock import MagicMock, patch

# 在 import 业务模块前设置假配置
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import pytest

from app.core import context
from app.core.context import (
    request_id_var,
    reset_request_id,
    session_id_var,
    set_intent,
    set_request_id,
    set_session_id,
    set_user_id,
)
from app.core.logging import (
    ContextFilter,
    JSONFormatter,
    TextFormatter,
    setup_logging,
)


# =============================================================
# ContextVar
# =============================================================
class TestContextVars:
    """ContextVar 基本行为 + 嵌套隔离"""

    def test_default_values(self):
        """未设置时返回默认值（用独立副本测 default）"""
        from contextvars import copy_context
        ctx = copy_context()
        def check_defaults():
            assert request_id_var.get() == "-"
            assert session_id_var.get() == "-"
            assert context.user_id_var.get() == 0
            assert context.intent_var.get() == "-"
        ctx.run(check_defaults)

    def test_set_get_reset(self):
        from contextvars import copy_context
        ctx = copy_context()
        def check():
            token = set_request_id("test-rid-123")
            try:
                assert request_id_var.get() == "test-rid-123"
            finally:
                reset_request_id(token)
            assert request_id_var.get() == "-"
        ctx.run(check)

    def test_user_id_setter(self):
        # 登录用户
        token = set_user_id(42)
        try:
            assert context.user_id_var.get() == 42
        finally:
            context.reset_user_id(token)
        assert context.user_id_var.get() == 0

        # None → 0（匿名）
        token2 = set_user_id(None)
        try:
            assert context.user_id_var.get() == 0
        finally:
            context.reset_user_id(token2)

    def test_session_id_setter_none(self):
        token = set_session_id(None)
        try:
            assert session_id_var.get() == "-"
        finally:
            context.reset_session_id(token)

    def test_intent_setter(self):
        token = set_intent("refund_query")
        try:
            assert context.intent_var.get() == "refund_query"
        finally:
            context.reset_intent(token)

    def test_get_all_returns_dict(self):
        token_r = set_request_id("req-1")
        token_s = set_session_id("sess-1")
        token_u = set_user_id(99)
        token_i = set_intent("order_query")
        try:
            all_ctx = context.get_all()
            assert all_ctx == {
                "request_id": "req-1",
                "session_id": "sess-1",
                "user_id": 99,
                "intent": "order_query",
            }
        finally:
            context.reset_intent(token_i)
            context.reset_user_id(token_u)
            context.reset_session_id(token_s)
            reset_request_id(token_r)

    def test_contextvars_are_per_task(self):
        """asyncio task 间 ContextVar 隔离"""
        async def task_set(value):
            set_request_id(value)
            await asyncio.sleep(0.01)
            return request_id_var.get()

        async def main():
            t1 = asyncio.create_task(task_set("task-1"))
            t2 = asyncio.create_task(task_set("task-2"))
            r1 = await t1
            r2 = await t2
            return r1, r2

        r1, r2 = asyncio.run(main())
        assert r1 == "task-1"
        assert r2 == "task-2"


# =============================================================
# JSONFormatter
# =============================================================
class TestJSONFormatter:
    """JSON 日志格式正确性"""

    def _make_record(self, msg="test", **extra) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_basic_format(self):
        fmt = JSONFormatter()
        record = self._make_record("hello")
        output = fmt.format(record)
        obj = json.loads(output)
        assert obj["msg"] == "hello"
        assert obj["level"] == "INFO"
        assert obj["logger"] == "test.logger"
        assert "ts" in obj
        # ts 形如 2026-06-28T10:23:45.123+08:00
        assert "+08:00" in obj["ts"]

    def test_extra_fields_included(self):
        fmt = JSONFormatter()
        record = self._make_record("op done", latency_ms=1850, hits=4)
        output = fmt.format(record)
        obj = json.loads(output)
        assert obj["latency_ms"] == 1850
        assert obj["hits"] == 4

    def test_context_fields_included(self):
        """context_filter 注入的字段会被序列化"""
        fmt = JSONFormatter()
        record = self._make_record("chat done")
        # 模拟 ContextFilter 注入
        record.request_id = "req-abc123"
        record.session_id = "sess-xyz"
        record.user_id = 7
        record.intent = "refund_query"
        output = fmt.format(record)
        obj = json.loads(output)
        assert obj["request_id"] == "req-abc123"
        assert obj["session_id"] == "sess-xyz"
        assert obj["user_id"] == 7
        assert obj["intent"] == "refund_query"

    def test_unicode_chinese_safe(self):
        fmt = JSONFormatter()
        record = self._make_record("退款订单 123 处理完成")
        output = fmt.format(record)
        obj = json.loads(output)
        assert obj["msg"] == "退款订单 123 处理完成"

    def test_extra_with_unserializable_value_stringified(self):
        fmt = JSONFormatter()
        record = self._make_record("test")
        # object() 不可 JSON 序列化 → 应该 fallback 到 str
        record.weird_obj = object()
        output = fmt.format(record)
        obj = json.loads(output)
        assert "weird_obj" in obj
        assert isinstance(obj["weird_obj"], str)


# =============================================================
# ContextFilter
# =============================================================
class TestContextFilter:
    """Filter 自动注入 ContextVar 到 LogRecord"""

    def test_filter_injects_context(self):
        flt = ContextFilter()
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="x", lineno=1,
            msg="m", args=(), exc_info=None,
        )
        token_r = set_request_id("req-filter-test")
        token_s = set_session_id("sess-filter")
        token_u = set_user_id(3)
        token_i = set_intent("policy_query")
        try:
            assert flt.filter(record) is True
            assert record.request_id == "req-filter-test"
            assert record.session_id == "sess-filter"
            assert record.user_id == 3
            assert record.intent == "policy_query"
        finally:
            context.reset_intent(token_i)
            context.reset_user_id(token_u)
            context.reset_session_id(token_s)
            reset_request_id(token_r)


# =============================================================
# setup_logging
# =============================================================
class TestSetupLogging:
    """setup_logging 不抛异常 + root logger 配置正确"""

    def test_text_format(self):
        setup_logging(level="INFO", log_format="text")
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) >= 1

    def test_json_format(self):
        setup_logging(level="DEBUG", log_format="json")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        handler = root.handlers[0]
        assert isinstance(handler.formatter, JSONFormatter)


# =============================================================
# Metrics 计数器
# =============================================================
class TestMetricsCounters:
    """指标计数器 + 直方图 + 线程安全"""

    def _fresh_metrics(self):
        """返回新的 Metrics 实例（避免单例污染）"""
        from app.services.metrics import Metrics
        return Metrics()

    def test_chat_increments(self):
        m = self._fresh_metrics()
        m.inc_chat("policy_query")
        m.inc_chat("policy_query")
        m.inc_chat("refund_query", v3_engine="v3")
        m.inc_chat("refund_query", v3_engine="v2")
        snap = m.snapshot()
        assert snap["chat"]["total"] == 4
        assert snap["chat"]["by_intent"]["policy_query"] == 2
        assert snap["chat"]["by_intent"]["refund_query"] == 2
        assert snap["chat"]["by_v3_engine"]["v3"] == 1
        assert snap["chat"]["by_v3_engine"]["v2"] == 1

    def test_latency_percentile(self):
        m = self._fresh_metrics()
        for lat in [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]:
            m.record_chat_latency(lat)
        snap = m.snapshot()
        lat = snap["chat"]["latency_ms"]
        assert lat["p50"] == 550.0  # 中位数
        assert lat["p90"] == 910.0  # 90 分位
        assert lat["max"] == 1000.0
        assert lat["samples"] == 10

    def test_answer_tokens_accumulate(self):
        m = self._fresh_metrics()
        m.record_answer_tokens(100)
        m.record_answer_tokens(50)
        assert m.snapshot()["chat"]["answer_tokens_total"] == 150

    def test_retrieve_hits_average(self):
        m = self._fresh_metrics()
        m.record_retrieve_hits(5)
        m.record_retrieve_hits(3)
        m.record_retrieve_hits(4)
        snap = m.snapshot()
        assert snap["chat"]["retrieve_hits_avg"] == 4.0

    def test_qdrant_outcomes(self):
        m = self._fresh_metrics()
        m.inc_qdrant_search("success")
        m.inc_qdrant_search("success")
        m.inc_qdrant_search("fallback_open")
        m.inc_qdrant_search("error")
        snap = m.snapshot()
        rag = snap["rag"]
        assert rag["qdrant_search_total"] == 4
        assert rag["qdrant_search_success"] == 2
        assert rag["qdrant_fallback_open_total"] == 1
        assert rag["qdrant_error_total"] == 1

    def test_embedding_outcomes(self):
        m = self._fresh_metrics()
        m.inc_embedding("success")
        m.inc_embedding("retry")
        m.inc_embedding("retry")
        m.inc_embedding("error")
        snap = m.snapshot()
        emb = snap["embedding"]
        assert emb["calls_total"] == 4
        assert emb["retries_total"] == 2
        assert emb["errors_total"] == 1

    def test_hit_at_k_calculation(self):
        """hit@K：rank 在 [1, K] 内算命中"""
        m = self._fresh_metrics()
        # 模拟 10 次检索：rank 分布 [1, 2, 3, 5, 0, 1, 8, 0, 1, 4]
        for rank in [1, 2, 3, 5, 0, 1, 8, 0, 1, 4]:
            m.record_hit_at_k(rank)
        snap = m.snapshot()
        hk = snap["hit_at_k"]
        # rank == 1 → 3 次（位置 0, 5, 8）/ 10 = 0.3
        assert hk["hit@1"] == 0.3
        # rank in {1,2,3} → 5 次（位置 0,1,2,5,8）/ 10 = 0.5
        assert hk["hit@3"] == 0.5
        # rank in {1..5} → 7 次（位置 0,1,2,3,5,8,9）/ 10 = 0.7
        assert hk["hit@5"] == 0.7
        # rank != 0 → 8 次（rank=0 的两次未命中）/ 10 = 0.8
        assert hk["hit@10"] == 0.8

    def test_hit_at_k_ring_buffer_limit(self):
        """ring buffer 只保留最近 HIT_K_WINDOW 个样本"""
        m = self._fresh_metrics()
        # 填满 100 个全命中
        for _ in range(100):
            m.record_hit_at_k(1)
        # 再写 50 个全不命中
        for _ in range(50):
            m.record_hit_at_k(0)
        snap = m.snapshot()
        hk = snap["hit_at_k"]
        # window 只保留最近 100 个：50 个不命中 + 50 个命中 = hit@1 = 0.5
        assert hk["window_size"] == 100
        assert hk["hit@1"] == 0.5
        # total_samples 累计
        assert hk["total_samples"] == 150

    def test_metrics_thread_safe(self):
        """多线程并发写计数器不丢更新"""
        m = self._fresh_metrics()
        N_THREADS = 10
        N_PER_THREAD = 1000

        # barrier 确保所有线程同时开始
        barrier = threading.Barrier(N_THREADS)

        def worker():
            barrier.wait()
            for _ in range(N_PER_THREAD):
                m.inc_chat("policy_query")
                m.record_chat_latency(100.0)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = m.snapshot()
        # 计数器无上限，必等于总写入
        assert snap["chat"]["total"] == N_THREADS * N_PER_THREAD
        # 直方图 deque maxlen=1000，所以保留的是最后 1000 个样本
        assert snap["chat"]["latency_ms"]["samples"] == m.LATENCY_WINDOW

    def test_snapshot_circuit_breaker_passthrough(self):
        m = self._fresh_metrics()
        cb_stats = {"qdrant": {"state": "closed", "failure_count": 0}}
        snap = m.snapshot(circuit_breaker_stats=cb_stats)
        assert snap["circuit_breaker"] == cb_stats

    def test_snapshot_uptime_increases(self):
        m = self._fresh_metrics()
        s1 = m.snapshot()["uptime_seconds"]
        time.sleep(0.05)
        s2 = m.snapshot()["uptime_seconds"]
        assert s2 >= s1


# =============================================================
# RequestIdMiddleware（用 FastAPI TestClient 测）
# =============================================================
class TestRequestIdMiddleware:
    """中间件：生成 / 透传 / 响应头"""

    def _build_app(self):
        """构建带双 middleware 的测试 app（路由由调用方添加）"""
        from fastapi import FastAPI
        from app.api.middleware import RequestIdMiddleware, ResponseHeaderMiddleware
        app = FastAPI()
        # 注册顺序：ResponseHeader 在外层（后 add_middleware 先执行）
        app.add_middleware(ResponseHeaderMiddleware)
        app.add_middleware(RequestIdMiddleware)
        return app

    def test_generates_request_id_when_missing(self):
        from fastapi.testclient import TestClient

        app = self._build_app()

        @app.get("/echo")
        async def echo():
            return {"rid": request_id_var.get()}

        client = TestClient(app)
        resp = client.get("/echo")
        assert resp.status_code == 200
        # 响应头必须有 X-Request-Id
        assert "x-request-id" in resp.headers
        rid = resp.headers["x-request-id"]
        assert rid.startswith("req-")
        # handler 内能读到同一个 rid
        assert resp.json()["rid"] == rid

    def test_honors_incoming_request_id(self):
        from fastapi.testclient import TestClient

        app = self._build_app()

        @app.get("/echo")
        async def echo():
            return {"rid": request_id_var.get()}

        client = TestClient(app)
        incoming = "my-trace-id-12345"
        resp = client.get("/echo", headers={"X-Request-Id": incoming})
        assert resp.headers["x-request-id"] == incoming
        assert resp.json()["rid"] == incoming

    def test_access_log_skips_health_metrics(self):
        """/health 和 /metrics 不写访问日志"""
        from fastapi.testclient import TestClient

        app = self._build_app()

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/metrics")
        async def metrics():
            return {}

        @app.get("/chat")
        async def chat():
            return {"ok": True}

        client = TestClient(app)
        # 抓取 log
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)

        client.get("/health")
        client.get("/metrics")
        client.get("/chat")

        log_content = log_stream.getvalue()
        assert "GET /chat 200" in log_content
        assert "GET /health 200" not in log_content
        assert "GET /metrics 200" not in log_content


# =============================================================
# /metrics 端点
# =============================================================
class TestMetricsEndpoint:
    """/metrics 端点返回 JSON 完整字段"""

    def test_metrics_endpoint_shape(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.middleware import RequestIdMiddleware, ResponseHeaderMiddleware
        from app.services.metrics import metrics as metrics_singleton

        app = FastAPI()
        app.add_middleware(ResponseHeaderMiddleware)
        app.add_middleware(RequestIdMiddleware)

        @app.get("/metrics-test")
        async def mt():
            return metrics_singleton.snapshot(circuit_breaker_stats={})

        # 故意做一些事让指标非零
        metrics_singleton.inc_chat("policy_query")
        metrics_singleton.record_chat_latency(123.0)

        client = TestClient(app)
        resp = client.get("/metrics-test")
        assert resp.status_code == 200
        data = resp.json()

        # 必须字段
        for key in ("uptime_seconds", "chat", "rag", "embedding", "hit_at_k"):
            assert key in data
        # chat 子字段
        for key in ("total", "by_intent", "by_v3_engine", "latency_ms",
                    "answer_tokens_total", "retrieve_hits_avg"):
            assert key in data["chat"]
        # hit_at_k 子字段
        for key in ("window_size", "hit@1", "hit@3", "hit@5", "hit@10"):
            assert key in data["hit_at_k"]