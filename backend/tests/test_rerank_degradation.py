"""V13（降级显形，2026-10-10）：rerank 失败/全 0 分必须被计数与告警

背景（当晚模型快照炸弹）：QWEN_MODEL 日期版本被 DashScope 退役返回 404，
rerank 在"except→全 0 分保原序"分支下静默空转——评测数字看似正常，
直到 hybrid 与 hybrid+rerank 两级结果逐项全等才暴露。
本测试锁定：①异常降级计 degraded；②全 0 分（解析失败形态）计 degraded；
③正常打分计 ok；④降级时保持原始顺序（行为不变，只是不许再静默）。
"""
import os

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x")

from unittest.mock import patch

from app.services.metrics import metrics


def _cands(n=3):
    return [
        {
            "id": f"p{i}",
            "score": 0.9 - i * 0.1,
            "payload": {"text": f"doc {i} 内容", "source": f"s{i}"},
        }
        for i in range(n)
    ]


def _provider():
    from app.core.providers.rerank.qwen_provider import QwenRerankProvider
    return QwenRerankProvider()


class TestRerankDegradationVisible:

    def test_exception_counts_degraded_and_keeps_order(self):
        """调用抛异常 → degraded+1，顺序保持原样（行为兼容，故障显形）"""
        b_t, b_d = metrics.rerank_total, metrics.rerank_degraded
        with patch(
            "app.core.providers.rerank.qwen_provider._legacy_qwen.chat",
            side_effect=RuntimeError("404 model not exist"),
        ):
            out = _provider().rerank("退货运费", _cands(), top_n=3)
        assert metrics.rerank_total == b_t + 1
        assert metrics.rerank_degraded == b_d + 1
        assert [c["id"] for c in out] == [f"p{i}" for i in range(3)]

    def test_all_zero_scores_counts_degraded(self):
        """LLM 返回无法解析（全 0 分形态）→ 同样计 degraded，不许静默"""
        b_t, b_d = metrics.rerank_total, metrics.rerank_degraded
        with patch(
            "app.core.providers.rerank.qwen_provider._legacy_qwen.chat",
            return_value={"reply": "抱歉我不知道怎么打分"},
        ):
            out = _provider().rerank("退货运费", _cands(), top_n=3)
        assert metrics.rerank_total == b_t + 1
        assert metrics.rerank_degraded == b_d + 1
        assert all(c["rerank_score"] == 0 for c in out)

    def test_success_counts_ok(self):
        """正常打分 → 只计 total 不计 degraded，且按分重排"""
        b_t, b_d = metrics.rerank_total, metrics.rerank_degraded
        with patch(
            "app.core.providers.rerank.qwen_provider._legacy_qwen.chat",
            return_value={"reply": '[{"id": 2, "score": 9}, {"id": 0, "score": 2}, {"id": 1, "score": 1}]'},
        ):
            out = _provider().rerank("退货运费", _cands(), top_n=3)
        assert metrics.rerank_total == b_t + 1
        assert metrics.rerank_degraded == b_d
        assert out[0]["id"] == "p2"

    def test_snapshot_exposes_rerank_block(self):
        snap = metrics.snapshot()
        assert "rerank_total" in snap["rag"]
        assert "rerank_degraded" in snap["rag"]
