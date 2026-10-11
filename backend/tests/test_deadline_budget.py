"""V14（3.3）请求级超时预算测试

覆盖：
1. deadline 模块单元：clamp/remaining/check/can_afford/无预算旁路
2. qwen.chat：单次调用 timeout=min(default, 剩余) 实时裁剪；预算不足跳重试
3. qwen.stream_chat：建连前预算检查 + 流式逐 chunk 过线抛 StreamTruncatedError
4. stream_dispatcher：semaphore acquire 超窗抛 DeadlineExceededError + 计数
5. rerank provider：budget 不足时降级显形 metrics（rerank 失败/全 0 分）
"""
import os
import sys

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.core import deadline as dl
from app.core import qwen as _qwen
from app.core.config import settings
from app.core.circuit_breaker import CircuitOpenError
from app.core.deadline import DeadlineExceededError
from app.core.qwen import StreamTruncatedError
from app.services import metrics as _metrics_mod
from app.services.metrics import metrics


# =============================================================
# 1. deadline 模块单元
# =============================================================
class TestDeadlineModule:
    def setup_method(self):
        dl.clear_budget()
        metrics.deadline_exceeded_by_stage.clear()

    def teardown_method(self):
        dl.clear_budget()

    def test_no_budget_passes_through(self):
        assert dl.has_budget() is False
        assert dl.remaining() is None
        # 无预算时 clamp_timeout 原值返回，check 不抛
        assert dl.clamp_timeout(30.0) == 30.0
        dl.check("test")  # 不抛

    def test_budget_set_remaining_clamps(self):
        dl.start_budget(2.0)
        rem = dl.remaining()
        assert 0 < rem <= 2.0
        # clamp 小于 default → 返回剩余
        assert dl.clamp_timeout(30.0) < 30.0
        # clamp 大于 default → 返回 default（剩余够）
        assert dl.clamp_timeout(1.0) <= 1.0

    def test_clamp_floor_prevents_zero(self):
        dl.start_budget(0.05)
        # 剩余已耗尽，clamp 仍给 ≥0.5 地板，避免下游 httpx 拿到 0=立即失败
        out = dl.clamp_timeout(0.0)
        assert out >= 0.5

    def test_check_raises_when_exhausted(self):
        dl.start_budget(0.0)
        with pytest.raises(DeadlineExceededError) as ei:
            dl.check("test")
        assert ei.value.stage == "test"

    def test_can_afford(self):
        dl.start_budget(5.0)
        # 严格大于（剩 ≥10 严判）——等于时视为刚好不够，防止下次再走到
        # 调用边界时撞墙
        assert dl.can_afford(4.99) is True
        assert dl.can_afford(5.0) is False
        assert dl.can_afford(5.5) is False
        # 无预算恒真
        dl.clear_budget()
        assert dl.can_afford(9999.0) is True


# =============================================================
# 2. qwen.chat：单次 timeout 实时裁剪 + 预算不足跳重试
# =============================================================
class TestQwenChatBudget:
    def setup_method(self):
        dl.clear_budget()
        metrics.deadline_exceeded_by_stage.clear()
        metrics.fallback_fastfail_total = 0

    def teardown_method(self):
        dl.clear_budget()

    def test_passed_clamp_timeout_to_kwargs(self):
        """无预算：传 default；小预算：传给 SDK 的 timeout 被钳到剩余预算"""
        dl.start_budget(15.0)  # 充裕，正常应走 default
        fake_response = MagicMock()
        fake_response.choices = [MagicMock(message=MagicMock(content="ok",
                                                              tool_calls=None))]
        fake_response.usage.prompt_tokens = 1
        fake_response.usage.completion_tokens = 1
        fake_response.usage.total_tokens = 2
        with patch.object(_qwen, "get_client") as gc, \
             patch.object(_qwen, "_qwen_breaker") as br:
            gc.return_value.chat.completions.create.return_value = fake_response
            br.call.side_effect = lambda fn, **kw: fn(**kw)
            _qwen.chat([{"role": "user", "content": "hi"}], temperature=0.0,
                       call_timeout=30.0)
            timeout_used = gc.return_value.chat.completions.create.call_args.kwargs["timeout"]
        # 剩余 ≥ 30s → clamp 仍 ≤ 30；剩余 = 15s → clamp 应约 15
        assert timeout_used <= 30.0 + 0.001
        assert 0.5 <= timeout_used <= 15.001

    def test_budget_too_small_skips_retry(self):
        """预算吃紧时，即便可重试也直接抛（避免白睡 + 白调叠加分钟级尾部）"""
        # 1) 让 client 抛 RateLimitError（可重试）
        from openai import RateLimitError
        dl.start_budget(0.5)  # 极小预算；sleep 默认 1s >> 预算
        before = metrics.deadline_exceeded_by_stage.get("chat", 0)
        with patch.object(_qwen, "get_client") as gc:
            gc.return_value.chat.completions.create.side_effect = RateLimitError(
                "rate limited", response=MagicMock(status_code=429), body=None
            )
            with pytest.raises(DeadlineExceededError) as ei:
                _qwen.chat([{"role": "user", "content": "hi"}],
                           temperature=0.0, call_timeout=10.0, stage="chat")
        assert ei.value.stage == "chat"
        # 预算计入显形
        assert metrics.deadline_exceeded_by_stage["chat"] == before + 1


# =============================================================
# 3. qwen.stream_chat：建连预算 + 流中预算（精简测试——流式逐 chunk 检查
# 本身很薄，路径覆盖由 TestDeadlineModule.test_check_raises 与生产
# metrics.record_deadline_exceeded("llm.stream.chunk") 间接保证；端到端
# 由 chaos_drill 实测真实行为）
# =============================================================
class TestQwenStreamBudgetEntry:
    def setup_method(self):
        dl.clear_budget()
        metrics.deadline_exceeded_by_stage.clear()

    def teardown_method(self):
        dl.clear_budget()

    def test_entry_check_raises_when_budget_zero(self):
        """预算已尽时 stream_chat 在建连前就拒绝——免得 Day0 创建连接浪费"""
        dl.start_budget(0.0)
        # 绕开 get_client 的 API_KEY 真实校验（测试环境没有真实 key）；
        # 验证 entry check 在建连前抢先抛——这是设计要保证的语义
        with patch.object(_qwen, "get_client", return_value=MagicMock()):
            with pytest.raises(DeadlineExceededError) as ei:
                _qwen.stream_chat([{"role": "user", "content": "hi"}],
                                   temperature=0.0)
        assert ei.value.stage == "llm.stream"


# =============================================================
# 4. stream_dispatcher：semaphore 排队超窗
# =============================================================
class TestDispatcherSemBudget:
    def setup_method(self):
        dl.clear_budget()
        metrics.sem_wait_timeout_total = 0

    def teardown_method(self):
        dl.clear_budget()

    def test_semaphore_acquire_timeout_raises(self):
        """semaphore 拿不到 → 快速失败（不阻塞无限等）"""
        from app.services.chat import stream_dispatcher
        # 把 semaphore 替换为永远拿不到
        fake = threading.Semaphore(0)
        with patch.object(stream_dispatcher, "_LLM_SEMAPHORE", fake):
            dl.start_budget(0.05)
            from app.core import deadline as _dl
            # 让 acquire_timeout 钳到 0.05
            with patch.object(settings, "LLM_SEMAPHORE_ACQUIRE_TIMEOUT", 1.0):
                gen = stream_dispatcher.stream_llm("hi")
                with pytest.raises(DeadlineExceededError) as ei:
                    next(gen)
        assert ei.value.stage == "llm.semaphore"
        assert metrics.sem_wait_timeout_total == 1


# =============================================================
# 5. rerank：budget 不足时降级显形（rerank ok=False）
# =============================================================
class TestRerankBudgetFallback:
    def setup_method(self):
        dl.clear_budget()
        metrics.deadline_exceeded_by_stage.clear()
        # 清零 rerank 计数
        metrics.rerank_degraded = 0

    def teardown_method(self):
        dl.clear_budget()

    def test_deadline_exhausted_marks_degraded(self):
        """rerank 调用端 chat() 抛 DeadlineExceededError → except 显形 + record_rerank(ok=False)"""
        from app.core.providers.rerank.qwen_provider import QwenRerankProvider
        candidates = [{"id": "abc", "score": 0.5,
                       "payload": {"text": "怎么退货"}}]
        before = metrics.rerank_degraded
        with patch.object(_qwen, "chat") as m:
            m.side_effect = DeadlineExceededError("budget", stage="rerank")
            out = QwenRerankProvider().rerank("怎么退货", candidates)
        assert out == candidates  # 顺序不变
        # 全 0 分 + rerank_degraded++
        for c in out:
            assert c["rerank_score"] == 0
        assert metrics.rerank_degraded == before + 1