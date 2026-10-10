"""V13（1.2，2026-10-10）：流式中途断连 → truncated 标记 + 缓存隔离回归测试

根因（审计发现）：qwen.stream_chat 中途断连曾吞异常 return，上游 stream_llm
以为正常收尾 → 半截答案当完整答案落库/进语义缓存（0.95 相似命中反复供应残答）。
修复：qwen 抛 StreamTruncatedError → stream_llm 捕获后 done 事件带 truncated=True
→ chat.py 禁写缓存 + SSE/审计打标 + metrics 残答计数。
"""
import os

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")

from unittest.mock import patch

import pytest

from app.core.qwen import StreamTruncatedError
from app.services.chat.stream_dispatcher import stream_llm
from app.services.metrics import metrics


def _fake_stream_breaking():
    def _gen(*args, **kwargs):
        yield "退款政策是"
        yield "7 天无理由"
        raise StreamTruncatedError("stream truncated after 2 chunks", partial_chunks=2)
    return _gen


class TestStreamTruncated:

    def test_stream_llm_marks_truncated_done(self):
        """断连后：token 照常下发，done 带 truncated=True（不再伪装完整收尾）"""
        with patch("app.services.chat.stream_dispatcher.get_llm_provider") as mp:
            mp.return_value.stream_chat.side_effect = _fake_stream_breaking()
            events = list(stream_llm("退货政策"))

        assert events[0][0] == "token" and events[1][0] == "token"
        done = events[-1]
        assert done[0] == "done"
        assert done[1]["truncated"] is True
        assert done[1]["answer"] == "退款政策是7 天无理由"

    def test_stream_llm_normal_path_flag_false(self):
        """正常收尾：truncated=False（对照组，防标记恒真）"""
        with patch("app.services.chat.stream_dispatcher.get_llm_provider") as mp:
            def _gen(*a, **k):
                yield "完整答案"
            mp.return_value.stream_chat.side_effect = _gen
            events = list(stream_llm("q"))
        assert events[-1][1]["truncated"] is False

    def test_metrics_counts_truncated(self):
        """残答计数入 metrics（可观测：truncated_rate 上线可查）"""
        before_t = metrics.llm_stream_total
        before_tr = metrics.llm_stream_truncated
        with patch("app.services.chat.stream_dispatcher.get_llm_provider") as mp:
            mp.return_value.stream_chat.side_effect = _fake_stream_breaking()
            list(stream_llm("q"))
        assert metrics.llm_stream_total == before_t + 1
        assert metrics.llm_stream_truncated == before_tr + 1

    def test_snapshot_exposes_stream_block(self):
        snap = metrics.snapshot()
        assert "stream_total" in snap["chat"]
        assert "stream_truncated" in snap["chat"]
