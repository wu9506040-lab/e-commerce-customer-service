"""
test_robustness.py - 健壮性加固测试（M7）

覆盖：
- CircuitBreaker 状态机：CLOSED → OPEN → HALF_OPEN → CLOSED
- Qdrant 降级：断路器开路时 search 返回 []
- embedding 降级：多次重试后抛 EmbeddingError
- SSE heartbeat：模拟客户端断开检测
"""
import asyncio
import time
import pytest
import os
from unittest.mock import patch, MagicMock, AsyncMock

# 在 import 业务模块前设置假 API Key（避免 get_client() 抛 ValueError）
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import pytest

from app.core.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


# =============================================================
# CircuitBreaker 状态机
# =============================================================
class TestCircuitBreakerStateMachine:
    """状态转换：CLOSED → OPEN → HALF_OPEN → CLOSED/OPEN"""

    def test_starts_closed(self):
        cb = CircuitBreaker(name="t1", failure_threshold=3, recovery_timeout=1.0)
        assert cb.state == CircuitState.CLOSED

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(name="t2", failure_threshold=3, recovery_timeout=1.0)

        def fail():
            raise ConnectionError("down")

        for _ in range(3):
            with pytest.raises(ConnectionError):
                cb.call(fail)

        assert cb.state == CircuitState.OPEN

    def test_open_raises_circuit_open_error(self):
        cb = CircuitBreaker(name="t3", failure_threshold=2, recovery_timeout=10.0)

        def fail():
            raise ConnectionError("down")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(fail)

        # 第 3 次调用直接抛 CircuitOpenError，不调实际函数
        with pytest.raises(CircuitOpenError) as exc:
            cb.call(fail)
        assert exc.value.retry_after > 0

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(name="t4", failure_threshold=2, recovery_timeout=0.1)

        def fail():
            raise ConnectionError("down")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(fail)

        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)  # 等过 recovery_timeout
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker(name="t5", failure_threshold=2, recovery_timeout=0.1)

        def fail():
            raise ConnectionError("down")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(fail)

        time.sleep(0.15)

        def succeed():
            return "ok"

        result = cb.call(succeed)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(name="t6", failure_threshold=2, recovery_timeout=0.1)

        def fail():
            raise ConnectionError("down")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(fail)
        time.sleep(0.15)
        # 探活失败
        with pytest.raises(ConnectionError):
            cb.call(fail)
        assert cb.state == CircuitState.OPEN

    def test_unexpected_exception_not_counted(self):
        """非 expected_exceptions 里的异常不计入失败计数"""
        cb = CircuitBreaker(
            name="t7",
            failure_threshold=2,
            recovery_timeout=10.0,
            expected_exceptions=(ConnectionError,),
        )

        def key_error():
            raise KeyError("oops")

        # KeyError 不计入
        for _ in range(5):
            with pytest.raises(KeyError):
                cb.call(key_error)

        assert cb.state == CircuitState.CLOSED

    def test_reset(self):
        cb = CircuitBreaker(name="t8", failure_threshold=2, recovery_timeout=10.0)

        def fail():
            raise ConnectionError("down")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                cb.call(fail)
        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.stats()["failure_count"] == 0

    def test_stats_export(self):
        cb = CircuitBreaker(name="t9", failure_threshold=5, recovery_timeout=30.0)
        stats = cb.stats()
        assert stats["name"] == "t9"
        assert stats["state"] == "closed"
        assert stats["failure_count"] == 0


# =============================================================
# Qdrant 降级
# =============================================================
class TestQdrantDegradation:
    """断路器开路时 qdrant search 返回 [] / upsert 返回 0"""

    def test_search_returns_empty_when_circuit_open(self):
        """模拟 Qdrant 连续失败 3 次 → 断路器开路 → search 返回 []"""
        from app.clients import qdrant
        from app.clients.qdrant import _qdrant_breaker
        _qdrant_breaker.reset()

        with patch.object(qdrant, 'get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.search.side_effect = ConnectionError("Qdrant down")
            mock_get_client.return_value = mock_client

            # 触发 3 次失败（每次都抛 ConnectionError，断路器累计计数）
            for _ in range(3):
                with pytest.raises(ConnectionError):
                    qdrant.search([0.1] * 1024, top_k=5)

            # 验证断路器已 OPEN
            assert _qdrant_breaker.state == CircuitState.OPEN

            # 第 4 次：断路器开路 → 返回 []（不调 client.search，不抛异常）
            result = qdrant.search([0.1] * 1024, top_k=5)
            assert result == []

        _qdrant_breaker.reset()

    def test_upsert_returns_zero_when_circuit_open(self):
        """断路器开路时 upsert 返回 0（让上层 MySQL 继续）"""
        from app.clients import qdrant
        from app.clients.qdrant import _qdrant_breaker
        from qdrant_client.models import PointStruct

        _qdrant_breaker.reset()

        with patch.object(qdrant, 'get_client') as mock_get_client:
            mock_client = MagicMock()
            mock_client.upsert.side_effect = ConnectionError("Qdrant down")
            mock_get_client.return_value = mock_client

            for _ in range(3):
                with pytest.raises(ConnectionError):
                    qdrant.upsert_points([PointStruct(id=1, vector=[0.1]*1024, payload={})])

            assert _qdrant_breaker.state == CircuitState.OPEN

            # 断路器开路 → upsert 返回 0（不抛异常）
            result = qdrant.upsert_points([PointStruct(id=1, vector=[0.1]*1024, payload={})])
            assert result == 0

        _qdrant_breaker.reset()

    def test_health_check_returns_status(self):
        from app.clients import qdrant
        from app.clients.qdrant import _qdrant_breaker
        _qdrant_breaker.reset()

        with patch.object(qdrant, 'get_collection_info') as mock_info:
            mock_info.return_value = {
                "name": "knowledge_base",
                "vectors_count": 100,
                "points_count": 100,
                "status": "green",
                "vector_size": 1024,
            }
            result = qdrant.health_check()
            assert result["ok"] is True
            assert result["points_count"] == 100
            assert result["circuit"]["state"] == "closed"

        _qdrant_breaker.reset()


# =============================================================
# Embedding 降级
# =============================================================
class TestEmbeddingDegradation:
    """多次重试后抛 EmbeddingError"""

    def test_rate_limit_retries(self, monkeypatch):
        """429 触发指数退避重试"""
        from openai import RateLimitError
        from app.core import embedding

        call_count = [0]

        def mock_embeddings_create(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise RateLimitError("rate limited", response=MagicMock(), body={})
            # 第 3 次成功
            mock_resp = MagicMock()
            mock_resp.data = [MagicMock(embedding=[0.1] * 1024)]
            mock_resp.usage.prompt_tokens = 10
            return mock_resp

        # mock get_client 避免真实 init
        mock_client = MagicMock()
        mock_client.embeddings.create = mock_embeddings_create
        monkeypatch.setattr(embedding, 'get_client', lambda: mock_client)
        monkeypatch.setattr(embedding.time, 'sleep', lambda _: None)

        result = embedding.embed_text("test")
        assert len(result) == 1024
        assert call_count[0] == 3

    def test_total_failure_raises_embedding_error(self, monkeypatch):
        """重试 3 次后仍失败 → 抛 EmbeddingError"""
        from openai import RateLimitError
        from app.core import embedding

        def always_fail(*args, **kwargs):
            raise RateLimitError("rate limited", response=MagicMock(), body={})

        mock_client = MagicMock()
        mock_client.embeddings.create = always_fail
        monkeypatch.setattr(embedding, 'get_client', lambda: mock_client)
        monkeypatch.setattr(embedding.time, 'sleep', lambda _: None)

        with pytest.raises(embedding.EmbeddingError) as exc:
            embedding.embed_text("test")
        assert "重试" in str(exc.value) or "rate" in str(exc.value).lower()

    def test_embed_text_or_mock_returns_zero_on_failure(self, monkeypatch):
        """embed_text_or_mock 失败时返回零向量"""
        from openai import RateLimitError
        from app.core import embedding

        def always_fail(*args, **kwargs):
            raise RateLimitError("down", response=MagicMock(), body={})

        mock_client = MagicMock()
        mock_client.embeddings.create = always_fail
        monkeypatch.setattr(embedding, 'get_client', lambda: mock_client)
        monkeypatch.setattr(embedding.time, 'sleep', lambda _: None)

        result = embedding.embed_text_or_mock("test")
        assert result == [0.0] * 1024

    def test_empty_text_raises_value_error(self):
        from app.core import embedding
        with pytest.raises(ValueError):
            embedding.embed_text("")
        with pytest.raises(ValueError):
            embedding.embed_text("   ")


# =============================================================
# SSE 心跳 + 断开检测
# =============================================================
class TestSSEHeartbeat:
    """SSE generator 的 heartbeat + 断开检测"""

    def test_heartbeat_interval_constant(self):
        """SSE heartbeat 间隔 = 30s（小于 nginx 默认 60s）"""
        from app.api.chat import SSE_HEARTBEAT_INTERVAL
        assert SSE_HEARTBEAT_INTERVAL == 30.0

    def test_event_format(self):
        """_sse_format 输出标准 SSE 格式"""
        from app.api.chat import _sse_format
        result = _sse_format({"type": "test", "data": "hello"})
        assert result.startswith("data: ")
        assert result.endswith("\n\n")
        assert '"type": "test"' in result
        assert '"data": "hello"' in result

    def test_heartbeat_event_format(self):
        """heartbeat 事件包含 ts 字段"""
        from app.api.chat import _sse_format
        result = _sse_format({"type": "heartbeat", "ts": 1234567890})
        assert '"type": "heartbeat"' in result
        assert '"ts": 1234567890' in result

    def test_closed_event_format(self):
        """closed 事件标记流结束"""
        from app.api.chat import _sse_format
        result = _sse_format({"type": "closed"})
        assert '"type": "closed"' in result
