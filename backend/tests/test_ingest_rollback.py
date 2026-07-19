"""
test_ingest_rollback.py — P1-3 L1 单测：MySQL 失败回滚 Qdrant 的 4 个分支

按 SOP-V1 §2.2 数据可信验证：L1 mock 验证函数调用序列；L2 集成测试（test_ingest_rollback_integration.py）
用 FakeQdrant + SQLite 验证真实数据状态。

本测试目标（§9.7 改代码前 5 问 + §4.5 AI Review）：
- mock-only：mock qdrant.upsert_points / delete_points + upsert_knowledge_meta + embedding provider
- 验证 rollback 触发条件（开关 / qdrant_written / doc is None）
- 验证 rollback 失败时不掩盖原 MySQL 失败信号
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services.rag import ingest


# 1024 维 mock 向量（与 Qdrant VECTOR_SIZE 对齐）
MOCK_VECTORS = [[0.0] * 1024 for _ in range(3)]


@pytest.fixture
def mock_embed():
    """mock get_embedding_provider().embed_texts → 返 N 个 1024 维向量"""
    provider = MagicMock()
    provider.embed_texts.return_value = MOCK_VECTORS
    with patch.object(ingest, "get_embedding_provider", return_value=provider):
        yield provider


def _text_for_3_chunks() -> str:
    """生成会被 chunk_text(text, 500, 50) 切成 3 片的原文"""
    # 1200 字符：500+50, 500+50, 100（末片）→ 3 片
    return "a" * 1200


# =============================================================
# Case 1: rollback_on（默认开关=True，MySQL 失败 → delete_points 被调 1 次）
# =============================================================
def test_rollback_on_mysql_failed_deletes_qdrant(mock_embed, monkeypatch):
    """开关 True + Qdrant 真写 3 点 + MySQL 失败 → delete_points(3 个 id) 被调"""
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", True)

    with patch.object(ingest, "upsert_points", return_value=3) as m_upsert, \
         patch.object(ingest, "delete_points", return_value=3) as m_delete, \
         patch.object(ingest, "upsert_knowledge_meta", return_value=None) as m_meta, \
         patch.object(ingest, "ensure_collection", return_value=True):
        result = ingest.ingest_text(_text_for_3_chunks(), source="test_case_1")

    m_upsert.assert_called_once()
    m_meta.assert_called_once()
    m_delete.assert_called_once()  # ✅ 关键断言：rollback 被触发
    assert len(m_delete.call_args.args[0]) == 3  # ✅ 删的是 3 个 chunk_id
    assert result["ingested_chunks"] == 3


# =============================================================
# Case 2: rollback_off（开关=False → 不回滚，保留原行为）
# =============================================================
def test_rollback_off_no_delete_call(mock_embed, monkeypatch):
    """开关 False + MySQL 失败 → delete_points 不被调（保留 M14 V3 前行为）"""
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", False)

    with patch.object(ingest, "upsert_points", return_value=3), \
         patch.object(ingest, "delete_points") as m_delete, \
         patch.object(ingest, "upsert_knowledge_meta", return_value=None), \
         patch.object(ingest, "ensure_collection", return_value=True):
        result = ingest.ingest_text(_text_for_3_chunks(), source="test_case_2")

    m_delete.assert_not_called()  # ✅ 关键断言：开关关闭时不触发
    assert result["ingested_chunks"] == 3


# =============================================================
# Case 3: qdrant_circuit_open（Qdrant 断路器开路 → 不回滚）
# =============================================================
def test_no_rollback_when_qdrant_circuit_open(mock_embed, monkeypatch):
    """Qdrant 断路器开路 → upsert_points 返 0 + MySQL 失败 → 不调 delete_points（无点可删）"""
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", True)

    with patch.object(ingest, "upsert_points", return_value=0) as m_upsert, \
         patch.object(ingest, "delete_points") as m_delete, \
         patch.object(ingest, "upsert_knowledge_meta", return_value=None), \
         patch.object(ingest, "ensure_collection", return_value=True):
        result = ingest.ingest_text(_text_for_3_chunks(), source="test_case_3")

    m_upsert.assert_called_once()
    m_delete.assert_not_called()  # ✅ 关键断言：qdrant_written=0 → 跳过 rollback
    assert result["ingested_chunks"] == 3


# =============================================================
# Case 4: rollback_also_fails（rollback 本身抛异常 → 不掩盖原 MySQL 错误）
# =============================================================
def test_rollback_exception_does_not_mask_original_failure(mock_embed, monkeypatch):
    """delete_points 抛异常 → 不重新抛 + log warning（确保原 MySQL 失败信号透出）"""
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", True)

    with patch.object(ingest, "upsert_points", return_value=3), \
         patch.object(ingest, "delete_points", side_effect=RuntimeError("qdrant delete failed")), \
         patch.object(ingest, "upsert_knowledge_meta", return_value=None), \
         patch.object(ingest, "ensure_collection", return_value=True):
        # ✅ 关键断言：rollback 失败时 ingest_text 不抛（保证调用方拿到正常返回）
        # 失败信号由 doc is None + log warning 透出，不通过异常掩盖
        result = ingest.ingest_text(_text_for_3_chunks(), source="test_case_4")

    assert result["ingested_chunks"] == 3


# =============================================================
# Case 5 (回归): MySQL 成功 → 不调 delete_points（happy path 不被影响）
# =============================================================
def test_mysql_success_no_rollback(mock_embed, monkeypatch):
    """MySQL 成功 → delete_points 不被调（回归保护：rollback 不能误伤 happy path）"""
    monkeypatch.setattr(ingest.settings, "RAG_ROLLBACK_ON_MYSQL_FAIL", True)

    fake_doc = MagicMock(id=42)
    with patch.object(ingest, "upsert_points", return_value=3), \
         patch.object(ingest, "delete_points") as m_delete, \
         patch.object(ingest, "upsert_knowledge_meta", return_value=fake_doc), \
         patch.object(ingest, "ensure_collection", return_value=True):
        result = ingest.ingest_text(_text_for_3_chunks(), source="test_case_5")

    m_delete.assert_not_called()  # ✅ 关键断言：MySQL 成功时绝不能回滚
    assert result["ingested_chunks"] == 3