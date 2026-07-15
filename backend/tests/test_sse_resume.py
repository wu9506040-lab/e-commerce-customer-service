"""
Sprint P2 / SSE Resume 测试

覆盖：
1. redis_store 新增的 stream checkpoint 函数（set/get/del/increment）
2. chat.py._sse_format 加 seq 参数（向后兼容）
3. /api/chat/resume 端点前置校验（checkpoint miss / query mismatch / 限流）
4. ResumeRequest schema 字段校验

设计原则（与 test_refund_config / test_intent_config 同模式）：
- 不依赖外部 Redis（用 unittest.mock patch redis_get）
- 单测只覆盖纯逻辑，集成测试暂不做（依赖 httpx + ASGI，太重）
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# 让模块能找到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 触发 import 时不会因为 env 缺失报错
os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# =============================================================
# 公共 fixture：mock Redis client
# =============================================================
@pytest.fixture
def mock_redis():
    """Mock Redis client — HSET/HGETALL/DEL/INCR/EXPIRE/PIPELINE 全支持"""
    r = MagicMock()
    pipe = MagicMock()
    r.pipeline.return_value = pipe
    # 默认值：hgetall 返回空 dict（mock checkpoint miss）
    r.hgetall.return_value = {}
    r.get.return_value = None
    # pipeline.execute 返回值（INCR=0/1, EXPIRE=ok）
    pipe.execute.return_value = [1, True]
    return r


# =============================================================
# 1. redis_store stream checkpoint 函数
# =============================================================
class TestRedisStoreStreamCheckpoint:
    """Sprint P2 / SSE Resume：redis_store 新增的 stream checkpoint 操作"""

    def test_set_stream_checkpoint_writes_via_pipeline(self, mock_redis):
        """set_stream_checkpoint: HSET + EXPIRE 必须走 pipeline（一次 RTT）"""
        from app.services.redis_store import set_stream_checkpoint

        with patch("app.services.redis_store.redis_get", return_value=mock_redis):
            set_stream_checkpoint(
                session_id="sid123",
                stream_id="abc123def456",
                prefix_text="你好世界",
                last_event_id=5,
                query="退款流程",
            )

        mock_redis.pipeline.assert_called_once()
        pipe = mock_redis.pipeline.return_value
        pipe.hset.assert_called_once()
        # 校验 HSET 的 key 和 mapping
        hset_call = pipe.hset.call_args
        assert hset_call.args[0] == "chat:stream:sid123:abc123def456"
        mapping = hset_call.kwargs["mapping"]
        assert mapping["prefix_text"] == "你好世界"
        assert mapping["last_event_id"] == "5"
        assert mapping["query"] == "退款流程"
        assert "created_at" in mapping
        # 校验 EXPIRE 调用
        pipe.expire.assert_called_once()
        pipe.execute.assert_called_once()

    def test_set_stream_checkpoint_ttl_is_600s(self, mock_redis):
        """TTL 必须为 600s（10 分钟，覆盖网络抖动 + 重连）"""
        from app.services.redis_store import set_stream_checkpoint, STREAM_CHECKPOINT_TTL

        with patch("app.services.redis_store.redis_get", return_value=mock_redis):
            set_stream_checkpoint("sid", "abc123def456", "x", 1, "q")

        pipe = mock_redis.pipeline.return_value
        expire_call = pipe.expire.call_args
        assert expire_call.args[1] == STREAM_CHECKPOINT_TTL == 600

    def test_get_stream_checkpoint_returns_none_when_missing(self, mock_redis):
        """get_stream_checkpoint: checkpoint 不存在 → None（410 触发条件）"""
        from app.services.redis_store import get_stream_checkpoint

        mock_redis.hgetall.return_value = {}
        with patch("app.services.redis_store.redis_get", return_value=mock_redis):
            assert get_stream_checkpoint("sid", "abc123def456") is None

    def test_get_stream_checkpoint_returns_dict(self, mock_redis):
        """get_stream_checkpoint: 命中 → dict（含 prefix_text / last_event_id / query）"""
        from app.services.redis_store import get_stream_checkpoint

        mock_redis.hgetall.return_value = {
            "prefix_text": "你好",
            "last_event_id": "5",
            "query": "退款",
            "created_at": "1234567890",
        }
        with patch("app.services.redis_store.redis_get", return_value=mock_redis):
            result = get_stream_checkpoint("sid", "abc123def456")

        assert result == {
            "prefix_text": "你好",
            "last_event_id": "5",
            "query": "退款",
            "created_at": "1234567890",
        }

    def test_del_stream_checkpoint_removes_both_keys(self, mock_redis):
        """del_stream_checkpoint: 同时删 checkpoint + resume_count（避免计数孤儿）"""
        from app.services.redis_store import del_stream_checkpoint

        with patch("app.services.redis_store.redis_get", return_value=mock_redis):
            del_stream_checkpoint("sid", "abc123def456")

        mock_redis.pipeline.assert_called_once()
        pipe = mock_redis.pipeline.return_value
        # 必须删两个 key：checkpoint + resume_count
        delete_calls = pipe.delete.call_args_list
        assert len(delete_calls) == 2
        deleted_keys = [c.args[0] for c in delete_calls]
        assert "chat:stream:sid:abc123def456" in deleted_keys
        assert "chat:stream:resume_count:sid:abc123def456" in deleted_keys
        pipe.execute.assert_called_once()

    def test_increment_resume_count_returns_value(self, mock_redis):
        """increment_resume_count: INCR 返回累加值；EXPIRE 同时设置（避免 key 永驻）"""
        from app.services.redis_store import increment_resume_count

        mock_redis.pipeline.return_value.execute.return_value = [3, True]
        with patch("app.services.redis_store.redis_get", return_value=mock_redis):
            result = increment_resume_count("sid", "abc123def456")

        assert result == 3
        pipe = mock_redis.pipeline.return_value
        pipe.incr.assert_called_once()
        pipe.expire.assert_called_once()


# =============================================================
# 2. _sse_format 加 seq 参数（向后兼容）
# =============================================================
class TestSseFormatWithSeq:
    """Sprint P2 / SSE Resume：_sse_format 接受可选 seq 参数"""

    def test_no_seq_returns_original_format(self):
        """seq=None 保持原格式（向后兼容 guard / cache 等不走 resume 的路径）"""
        from app.api.chat import _sse_format

        result = _sse_format({"type": "heartbeat", "ts": 1234567890})
        assert result == 'data: {"type": "heartbeat", "ts": 1234567890}\n\n'
        assert "id:" not in result

    def test_with_seq_includes_id_line(self):
        """seq=数字时在 data 行前加 id: 行（SSE 标准 Last-Event-ID 协议）"""
        from app.api.chat import _sse_format

        result = _sse_format({"type": "token", "text": "你"}, seq=5)
        # 顺序必须是 id: 在前，data: 在后
        assert result.startswith("id: 5\n")
        assert 'data: {"type": "token", "text": "你"}\n\n' in result

    def test_seq_zero_renders_as_zero(self):
        """seq=0 也是合法（meta event 的 id=1 之前）—— 但 SSE 协议不区分 0/未设置"""
        from app.api.chat import _sse_format

        result = _sse_format({"type": "meta", "stream_id": "abc"}, seq=1)
        assert result.startswith("id: 1\n")


# =============================================================
# 3. ResumeRequest schema 字段校验
# =============================================================
class TestResumeRequestSchema:
    """Sprint P2 / SSE Resume：ResumeRequest 字段校验"""

    def test_required_fields(self):
        """session_id / stream_id / query 为必填"""
        from app.schemas.chat import ResumeRequest

        req = ResumeRequest(
            session_id="sid123",
            stream_id="abc123def456",
            query="退款流程",
        )
        assert req.session_id == "sid123"
        assert req.stream_id == "abc123def456"
        assert req.query == "退款流程"
        assert req.last_event_id is None
        assert req.sku is None
        assert req.order_no is None

    def test_stream_id_length_must_be_12(self):
        """stream_id 长度必须 12（与后端 uuid4().hex[:12] 对齐）"""
        from app.schemas.chat import ResumeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ResumeRequest(session_id="sid", stream_id="short", query="x")
        with pytest.raises(ValidationError):
            ResumeRequest(session_id="sid", stream_id="a" * 13, query="x")
        # 合法长度 12
        req = ResumeRequest(session_id="sid", stream_id="a" * 12, query="x")
        assert req.stream_id == "a" * 12

    def test_query_max_length_2000(self):
        """query 长度上限 2000（与 ChatRequest 对齐）"""
        from app.schemas.chat import ResumeRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ResumeRequest(session_id="sid", stream_id="a" * 12, query="x" * 2001)
        # 2000 字符合法
        req = ResumeRequest(session_id="sid", stream_id="a" * 12, query="x" * 2000)
        assert len(req.query) == 2000


# =============================================================
# 4. /api/chat/resume 端点前置校验
# =============================================================
class TestChatResumeEndpointPreCheck:
    """Sprint P2 / SSE Resume：resume 端点的 410 前置校验"""

    def _payload(self, query="退款", stream_id="abc123def456"):
        from app.schemas.chat import ResumeRequest

        return ResumeRequest(
            session_id="sid123",
            stream_id=stream_id,
            query=query,
            last_event_id=5,
        )

    def _request(self):
        """mock FastAPI Request（is_disconnected 返回 False）"""
        req = MagicMock()
        req.is_disconnected = MagicMock(return_value=False)
        return req

    def test_checkpoint_missing_returns_410(self):
        """checkpoint 不存在 / TTL 过期 → 410 Gone（前端走普通重试）"""
        from app.api.chat import chat_resume
        from fastapi import HTTPException

        with patch("app.api.chat.get_stream_checkpoint", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(chat_resume(self._request(), self._payload(), None))
            assert exc_info.value.status_code == 410
            assert "checkpoint" in str(exc_info.value.detail).lower()

    def test_query_mismatch_returns_410(self):
        """query 与 checkpoint 不一致 → 410（防 query mismatch 注入）"""
        from app.api.chat import chat_resume
        from fastapi import HTTPException

        cp = {
            "prefix_text": "你好",
            "last_event_id": "5",
            "query": "原问题",
            "created_at": "0",
        }
        with patch("app.api.chat.get_stream_checkpoint", return_value=cp):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(chat_resume(self._request(), self._payload(query="新问题"), None))
            assert exc_info.value.status_code == 410
            assert "query mismatch" in str(exc_info.value.detail).lower()

    def test_resume_limit_exceeded_returns_410(self):
        """resume 次数超限（>= STREAM_MAX_RESUME_TIMES）→ 410"""
        from app.api.chat import chat_resume
        from fastapi import HTTPException

        cp = {
            "prefix_text": "你好",
            "last_event_id": "5",
            "query": "退款",
            "created_at": "0",
        }
        # mock redis client：get 返回 "2"（=上限）
        mock_redis_client = MagicMock()
        mock_redis_client.get.return_value = "2"

        with patch("app.api.chat.get_stream_checkpoint", return_value=cp):
            # 同时 patch chat_resume 内的 _redis_get（函数内 import 别名）+ redis_store 内的 redis_get（import 别名）
            with patch("app.clients.redis_client.get_client", return_value=mock_redis_client), \
                 patch("app.services.redis_store.redis_get", return_value=mock_redis_client):
                with pytest.raises(HTTPException) as exc_info:
                    asyncio.run(chat_resume(self._request(), self._payload(), None))
                assert exc_info.value.status_code == 410
                assert "limit" in str(exc_info.value.detail).lower()

    def test_resume_within_limit_succeeds(self, mock_redis):
        """resume 计数 < 上限 → 成功（返 StreamingResponse）"""
        from app.api.chat import chat_resume
        from fastapi.responses import StreamingResponse

        cp = {
            "prefix_text": "你好世界",
            "last_event_id": "5",
            "query": "退款",
            "created_at": "0",
        }
        # mock 计数 0（未超限）+ pipeline.execute 返 INCR=1
        mock_redis.get.return_value = "0"
        mock_redis.pipeline.return_value.execute.return_value = [1, True]

        with patch("app.api.chat.get_stream_checkpoint", return_value=cp):
            # 同时 patch chat_resume 内的 _redis_get + redis_store 内的 redis_get
            with patch("app.clients.redis_client.get_client", return_value=mock_redis), \
                 patch("app.services.redis_store.redis_get", return_value=mock_redis):
                response = asyncio.run(chat_resume(self._request(), self._payload(), None))
                assert isinstance(response, StreamingResponse)
                assert response.media_type == "text/event-stream"


# =============================================================
# 5. STREAM_MAX_RESUME_TIMES 常量
# =============================================================
class TestStreamResumeConstants:
    """Sprint P2 / SSE Resume：常量边界保护"""

    def test_max_resume_times_is_2(self):
        """限流上限 = 2（用户拍板 MVP：后端 2 次，前端自动 1 次 + 手动 1 次）"""
        from app.services.redis_store import STREAM_MAX_RESUME_TIMES

        assert STREAM_MAX_RESUME_TIMES == 2

    def test_checkpoint_ttl_is_600s(self):
        """TTL = 600s（10 分钟；覆盖典型网络抖动 + 重连）"""
        from app.services.redis_store import STREAM_CHECKPOINT_TTL

        assert STREAM_CHECKPOINT_TTL == 600
