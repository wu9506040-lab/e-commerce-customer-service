"""
test_chunk_id_stable.py — P1-1 L1 单测：chunk_id 基于内容 hash 的稳定性

按 SOP-V1 §2.2 数据可信验证：L1 mock 验证生成逻辑；
L2 集成测试（test_chunk_id_stable_integration.py）用 FakeQdrant 验证重跑幂等。

测试目标（覆盖旧逻辑的 bug 场景）：
- 旧：uuid5(source + ":" + i) → source 中增删 chunk → 后续 ID 整体偏移
- 新：uuid5(source + ":" + chunk_hash[:32]) → 同一文本永远同 ID
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings
from app.services.rag import ingest
from app.services.rag.ingest import compute_chunk_id


# =============================================================
# Case 1: 同内容同 ID（核心价值）
# 同一段文本 + 同 source → 永远同 chunk_id（与下标无关）
# =============================================================
def test_same_text_same_id_regardless_of_position():
    """同 source + 同文本 → 永远同 chunk_id（顺序无关）"""
    text = "退换货政策：7 天无理由退货，需保持商品完好。"

    # 模拟两次 ingest（text 在不同位置被 chunk_text 切到）：
    # 即便 compute_chunk_id 被调用时 position 不同，新逻辑下 ID 也相同
    id_call1 = compute_chunk_id("policy_return.json", text, position=0)
    id_call2 = compute_chunk_id("policy_return.json", text, position=1)
    id_call3 = compute_chunk_id("policy_return.json", text, position=99)

    # ✅ 核心断言：同文本同 source → 永远同 ID（无论下标）
    assert id_call1 == id_call2 == id_call3


# =============================================================
# Case 2: 内容变 ID 变（一字之差 → 不同 ID）
# =============================================================
def test_text_change_changes_id():
    """内容改一个字符 → chunk_id 不同（避免误判为"重复文本"）"""
    text_v1 = "7 天无理由退货，需保持商品完好。"
    text_v2 = "7 天无理由退货，需保持商品完整。"  # "完好" → "完整"

    id_v1 = compute_chunk_id("policy.json", text_v1)
    id_v2 = compute_chunk_id("policy.json", text_v2)

    assert id_v1 != id_v2


# =============================================================
# Case 3: 不同 source 同文本 → 不同 ID（避免跨 source 冲突）
# =============================================================
def test_different_source_same_text_different_id():
    """不同 source + 同文本 → 不同 chunk_id（避免跨文档串 ID）"""
    text = "7 天无理由退货"

    id_s1 = compute_chunk_id("policy_return.json", text)
    id_s2 = compute_chunk_id("policy_warranty.json", text)

    assert id_s1 != id_s2


# =============================================================
# Case 4: 开关关闭 → 回退旧逻辑（基于下标）
# =============================================================
def test_switch_off_uses_index_based_id(monkeypatch):
    """开关 False → 走旧逻辑（uuid5(source + ":" + i)），验证开关可关"""
    monkeypatch.setattr(settings, "RAG_CHUNK_ID_BY_CONTENT_HASH", False)

    text = "7 天无理由退货"
    id_pos0 = compute_chunk_id("policy.json", text, position=0)
    id_pos1 = compute_chunk_id("policy.json", text, position=1)

    # 旧逻辑：同文本但下标不同 → ID 不同
    assert id_pos0 != id_pos1


# =============================================================
# Case 5: 开关关闭 + 不传 position → 抛 ValueError（明确错误信号）
# =============================================================
def test_switch_off_without_position_raises(monkeypatch):
    """旧逻辑下不传 position → 抛 ValueError（防止 silent fallback）"""
    monkeypatch.setattr(settings, "RAG_CHUNK_ID_BY_CONTENT_HASH", False)

    with pytest.raises(ValueError, match="必须传 position"):
        compute_chunk_id("policy.json", "test text")


# =============================================================
# Case 6: chunk_id 是合法 UUID 字符串（Qdrant ID / 迁移脚本对接用）
# =============================================================
def test_id_format_is_valid_uuid():
    """chunk_id 输出始终是合法 UUID 字符串"""
    chunk_id = compute_chunk_id("policy.json", "test content")

    # 必须能被 uuid.UUID 解析
    parsed = uuid.UUID(chunk_id)
    # 必须是 str 类型
    assert isinstance(chunk_id, str)
    # 标准 UUID 字符串长度
    assert len(chunk_id) == 36
    # round-trip 一致
    assert str(parsed) == chunk_id


# =============================================================
# Case 7 (回归): ingest_text 用 compute_chunk_id 生成 ID
# =============================================================
def test_ingest_text_uses_compute_chunk_id():
    """ingest_text 内部走 compute_chunk_id（验证集成路径正确）"""
    # mock qdrant + meta + embed，捕获 PointStruct.id 看是否跟 compute_chunk_id 一致
    expected_id = compute_chunk_id("test_src", "a" * 500, position=0)
    captured_ids = []

    class FakeQdrant:
        def upsert(self, collection_name, points, **kwargs):
            for p in points:
                captured_ids.append(str(p.id))
            return {"status": "completed", "count": len(points)}

    provider = MagicMock()
    provider.embed_texts.side_effect = lambda texts: [[0.0] * 1024 for _ in texts]

    with patch.object(ingest, "get_embedding_provider", return_value=provider), \
         patch.object(ingest, "ensure_collection", return_value=True), \
         patch.object(ingest, "upsert_points", side_effect=lambda points, **kw: (captured_ids.extend(str(p.id) for p in points), len(points))[1]), \
         patch.object(ingest, "upsert_knowledge_meta", return_value=MagicMock(id=1)):
        ingest.ingest_text("a" * 600, source="test_src")  # 600 字符 → 切 2 片

    # ✅ 关键断言：ingest_text 生成的 chunk_id == compute_chunk_id 算出的一致
    assert expected_id in captured_ids
    assert len(captured_ids) == 2  # 600/500/50 → 2 片