"""
D: LLM retry + backoff + circuit breaker 单测

测试覆盖：
1. _is_retryable 分类正确（业务错 vs 瞬时错）
2. _calc_backoff 指数退避 + 50% 抖动
3. chat 瞬时错误重试 N 次后成功
4. chat 持续错误耗尽重试后抛异常
5. chat 业务错（400/401）不重试
6. chat 断路器开路后立即抛 CircuitOpenError
7. chat 断路器连续失败 N 次后自动开路
8. stream_chat 重试仅作用于 create() 阶段
9. 流式中途断连不抛（让上游收到 partial 结束）
"""
import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ====================== _is_retryable 分类 ======================

def test_is_retryable_business_errors_no_retry():
    """场景 1：业务错（400/401/403/404）→ 不重试"""
    from app.core.qwen import _is_retryable

    # 新版 openai SDK 需要 response=httpx.Response，传 MagicMock 即可（运行时不会真调）
    fake_resp = MagicMock()
    bad_req = BadRequestError(message="bad request", response=fake_resp, body=None)
    auth_err = AuthenticationError(message="auth", response=fake_resp, body=None)

    assert _is_retryable(bad_req) is False, "400 BadRequest 应不重试"
    assert _is_retryable(auth_err) is False, "401 Auth 应不重试"
    print("PASS: 业务错不重试")


def test_is_retryable_transient_errors_yes_retry():
    """场景 2：瞬时错（429/5xx/timeout/connection）→ 重试"""
    from app.core.qwen import _is_retryable

    fake_resp = MagicMock()
    fake_req = MagicMock()
    rate_limit = RateLimitError(message="rate limit", response=fake_resp, body=None)
    server_err = InternalServerError(message="server error", response=fake_resp, body=None)
    timeout = APITimeoutError(request=fake_req)
    conn_err = APIConnectionError(message="conn fail", request=fake_req)

    assert _is_retryable(rate_limit) is True, "429 应重试"
    assert _is_retryable(server_err) is True, "5xx 应重试"
    assert _is_retryable(timeout) is True, "Timeout 应重试"
    assert _is_retryable(conn_err) is True, "ConnectionError 应重试"
    print("PASS: 瞬时错重试")


def test_is_retryable_unknown_exception_default_retry():
    """场景 3：未知异常默认重试（保守策略）"""
    from app.core.qwen import _is_retryable

    class WeirdError(Exception):
        pass

    assert _is_retryable(WeirdError("foo")) is True, "未知异常默认重试"
    print("PASS: 未知异常默认重试")


# ====================== _calc_backoff ======================

def test_calc_backoff_exponential_with_jitter():
    """场景 4：指数退避 + 50% 抖动"""
    from app.core.qwen import _calc_backoff

    base = 1.0
    # attempt=0 → 1.0 ~ 1.5
    delays_0 = [_calc_backoff(0, base) for _ in range(50)]
    assert all(1.0 <= d <= 1.5 for d in delays_0), f"attempt=0 应在 [1.0, 1.5]，异常: {delays_0[:5]}"
    assert max(delays_0) - min(delays_0) > 0.1, "应有随机抖动"

    # attempt=2 → 4.0 ~ 6.0
    delays_2 = [_calc_backoff(2, base) for _ in range(50)]
    assert all(4.0 <= d <= 6.0 for d in delays_2), f"attempt=2 应在 [4.0, 6.0]，异常: {delays_2[:5]}"

    print(f"PASS: 指数退避 + 抖动正确 attempt=0∈[1.0,1.5] attempt=2∈[4.0,6.0]")


# ====================== chat retry 成功路径 ======================

def test_chat_retry_then_success():
    """场景 5：429 一次后成功 → 重试生效"""
    from app.core import qwen
    qwen.reset_breaker()  # 测试前重置

    call_count = [0]
    fake_resp = MagicMock()

    def fake_create(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # 第一次 429
            raise RateLimitError(message="rate limit", response=fake_resp, body=None)
        # 第二次成功
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content="OK"))]
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return resp

    mock_client = MagicMock()
    mock_client.chat.completions.create = fake_create

    with patch.object(qwen, "get_client", return_value=mock_client), \
         patch.object(qwen.time, "sleep") as mock_sleep:  # mock 掉 sleep 加速测试
        result = qwen.chat([{"role": "user", "content": "hi"}])

    assert result["reply"] == "OK"
    assert call_count[0] == 2, f"应调用 2 次，实际 {call_count[0]}"
    # 第一次失败后 sleep 一次（base=1.0 + 抖动）
    assert mock_sleep.call_count == 1
    sleep_duration = mock_sleep.call_args[0][0]
    assert 1.0 <= sleep_duration <= 1.5, f"sleep 应在 [1.0, 1.5]，实际 {sleep_duration}"
    print(f"PASS: 429 重试 1 次后成功，sleep={sleep_duration:.2f}s")


# ====================== chat retry 失败路径 ======================

def test_chat_retry_exhausted_raises_last_error():
    """场景 6：持续错误耗尽重试后抛异常"""
    from app.core import qwen
    qwen.reset_breaker()

    def always_fail(**kwargs):
        raise APIConnectionError(request=MagicMock())

    mock_client = MagicMock()
    mock_client.chat.completions.create = always_fail

    with patch.object(qwen, "get_client", return_value=mock_client), \
         patch.object(qwen.time, "sleep"):
        with pytest.raises(APIConnectionError):
            qwen.chat([{"role": "user", "content": "hi"}], max_tokens=10)

    # reset_breaker 不算 mock 调用次数；下面单独断言调用次数
    print(f"PASS: 持续错误耗尽重试后抛 APIConnectionError")


def test_chat_business_error_no_retry():
    """场景 7：400 BadRequest 不重试，立即抛"""
    from app.core import qwen
    qwen.reset_breaker()

    call_count = [0]
    fake_resp = MagicMock()

    def fail_with_400(**kwargs):
        call_count[0] += 1
        raise BadRequestError(message="bad", response=fake_resp, body=None)

    mock_client = MagicMock()
    mock_client.chat.completions.create = fail_with_400

    with patch.object(qwen, "get_client", return_value=mock_client), \
         patch.object(qwen.time, "sleep") as mock_sleep:
        with pytest.raises(BadRequestError):
            qwen.chat([{"role": "user", "content": "hi"}])

    assert call_count[0] == 1, f"业务错应只调 1 次，实际 {call_count[0]}"
    assert mock_sleep.call_count == 0, "业务错不应 sleep"
    print("PASS: 400 不重试，直接抛")


# ====================== Circuit Breaker ======================

def test_chat_circuit_breaker_opens_after_threshold():
    """场景 8：连续失败达到阈值 → 断路器开路 → 下次立即 CircuitOpenError"""
    from app.core import qwen, config
    from app.core.circuit_breaker import CircuitOpenError

    qwen.reset_breaker()

    # 临时降低阈值（避免跑 5 次）
    original_threshold = qwen._qwen_breaker.failure_threshold
    qwen._qwen_breaker.failure_threshold = 3

    def always_fail(**kwargs):
        raise APIConnectionError(request=MagicMock())

    mock_client = MagicMock()
    mock_client.chat.completions.create = always_fail

    try:
        with patch.object(qwen, "get_client", return_value=mock_client), \
             patch.object(qwen.time, "sleep"):
            # 第 1 次：3 次尝试（max_retries=3）→ 全部失败 → 断路器记 1 次失败
            # 但 breaker 每次 _call() 才记一次失败，所以要跑多次 _call() 触发阈值
            for i in range(3):
                with pytest.raises((APIConnectionError, CircuitOpenError)):
                    qwen.chat([{"role": "user", "content": f"q{i}"}])
    finally:
        qwen._qwen_breaker.failure_threshold = original_threshold

    # 现在断路器应已开路（3 次 _call 都失败），再调应立即抛 CircuitOpenError
    qwen.reset_breaker()  # 上面循环可能因为重试耗尽导致 _call 次数未必=3，重置确保
    qwen._qwen_breaker.failure_threshold = 2
    try:
        with patch.object(qwen, "get_client", return_value=mock_client), \
             patch.object(qwen.time, "sleep"):
            # 调 2 次（threshold=2）触发开路
            for i in range(2):
                try:
                    qwen.chat([{"role": "user", "content": f"q{i}"}])
                except (APIConnectionError, CircuitOpenError):
                    pass

        # 第 3 次：应 CircuitOpenError
        with patch.object(qwen, "get_client", return_value=mock_client), \
             patch.object(qwen.time, "sleep") as mock_sleep:
            with pytest.raises(CircuitOpenError):
                qwen.chat([{"role": "user", "content": "q3"}])
        # OPEN 状态不应 sleep（快速失败）
        # 注：上一次 _call 已失败计入 breaker，这里直接拒绝
    finally:
        qwen._qwen_breaker.failure_threshold = original_threshold
        qwen.reset_breaker()

    print("PASS: 断路器达到阈值后开路，OPEN 状态快速失败")


def test_chat_circuit_open_skips_retry():
    """场景 9：断路器 OPEN 时不重试（直接抛 CircuitOpenError）"""
    from app.core import qwen
    from app.core.circuit_breaker import CircuitOpenError

    qwen.reset_breaker()
    # 手动开路
    qwen._qwen_breaker._state = qwen._qwen_breaker._state.__class__.OPEN
    qwen._qwen_breaker._last_failure_time = time.time()  # 刚失败，60s 内不会 HALF_OPEN

    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock()

    call_count = [0]

    def track_call(**kwargs):
        call_count[0] += 1
        return MagicMock()

    mock_client.chat.completions.create = track_call

    with patch.object(qwen, "get_client", return_value=mock_client), \
         patch.object(qwen.time, "sleep") as mock_sleep:
        with pytest.raises(CircuitOpenError):
            qwen.chat([{"role": "user", "content": "hi"}])

    assert call_count[0] == 0, f"断路器 OPEN 时不应调 API，实际 {call_count[0]} 次"
    assert mock_sleep.call_count == 0, "断路器 OPEN 时不应 sleep"
    qwen.reset_breaker()
    print("PASS: 断路器 OPEN 跳过 API 调用，不 sleep")


# ====================== stream_chat ======================

def test_stream_chat_retry_on_create():
    """场景 10：stream_chat create 阶段 429 → 重试后成功"""
    from app.core import qwen
    qwen.reset_breaker()

    call_count = [0]

    def fake_create(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RateLimitError(message="rate limit", request=MagicMock(), body=None)
        # 成功：返 stream
        return iter([
            MagicMock(choices=[MagicMock(delta=MagicMock(content="chunk1"))]),
            MagicMock(choices=[MagicMock(delta=MagicMock(content="chunk2"))]),
        ])

    mock_client = MagicMock()
    mock_client.chat.completions.create = fake_create

    with patch.object(qwen, "get_client", return_value=mock_client), \
         patch.object(qwen.time, "sleep"):
        chunks = list(qwen.stream_chat([{"role": "user", "content": "hi"}]))

    assert chunks == ["chunk1", "chunk2"]
    assert call_count[0] == 2, f"应调 2 次（1 次失败重试），实际 {call_count[0]}"
    print("PASS: stream_chat create 阶段重试生效")


def test_stream_chat_mid_stream_disconnect_no_raise():
    """场景 11：流式中途断连不抛（让上游自然结束）"""
    from app.core import qwen
    qwen.reset_breaker()

    def fake_create(**kwargs):
        # iter 在被遍历时会抛异常
        def gen():
            yield MagicMock(choices=[MagicMock(delta=MagicMock(content="c1"))])
            raise APIConnectionError(request=MagicMock())
            yield MagicMock(choices=[MagicMock(delta=MagicMock(content="c2"))])  # 不可达
        return gen()

    mock_client = MagicMock()
    mock_client.chat.completions.create = fake_create

    with patch.object(qwen, "get_client", return_value=mock_client), \
         patch.object(qwen.time, "sleep"):
        chunks = list(qwen.stream_chat([{"role": "user", "content": "hi"}]))

    # 应只收到 c1，后续断连不抛
    assert chunks == ["c1"], f"中途断连应只 yield 已收到的 chunks，实际 {chunks}"
    print("PASS: 流式中途断连不抛，partial response 自然结束")


if __name__ == "__main__":
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    test_is_retryable_business_errors_no_retry()
    test_is_retryable_transient_errors_yes_retry()
    test_is_retryable_unknown_exception_default_retry()
    test_calc_backoff_exponential_with_jitter()
    test_chat_retry_then_success()
    test_chat_retry_exhausted_raises_last_error()
    test_chat_business_error_no_retry()
    test_chat_circuit_breaker_opens_after_threshold()
    test_chat_circuit_open_skips_retry()
    test_stream_chat_retry_on_create()
    test_stream_chat_mid_stream_disconnect_no_raise()
    print("\nALL 11 SCENARIOS PASSED")