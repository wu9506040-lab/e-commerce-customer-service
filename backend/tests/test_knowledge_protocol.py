"""Sprint 17: KnowledgeSource Protocol 测试契约（spec §3.4 · 6 用例）

mock Qdrant Client / Embedding，验证：
1. search 返回 SearchResult 字段全对
2. filters 生效（score_threshold / coarse_top_k）
3. get_document doc_id 不存在 → None
4. upsert 返回 doc_id（占位行为）
5. PolicyService 改用 Protocol（mock 替换 _get_knowledge_source）
6. factory.get("qdrant") → QdrantKnowledgeSource 实例
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# 必须在 import app.* 之前 setdefault（与现有测试一致）
import os
os.environ.setdefault(
    "JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
)
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4",
)
os.environ.setdefault("APP_ENV", "test")

from app.schemas.knowledge import SearchResult, Document
from app.rag.protocols import KnowledgeError, DocumentNotFoundError
from app.rag.qdrant_impl import QdrantKnowledgeSource
from app.rag.factory import get_knowledge_source, reset_knowledge_source


# =============================================================
# 1. test_qdrant_search_returns_results
# =============================================================
@pytest.mark.asyncio
async def test_qdrant_search_returns_results():
    """mock embedding + qdrant.search → QdrantKnowledgeSource.search 返回 SearchResult 列表"""
    # 准备 mock 数据（Qdrant 返回结构）
    mock_hits = [
        {
            "id": "doc-001",
            "score": 0.92,
            "payload": {
                "text": "退货政策：7 天无理由退货",
                "source": "policy_return",
                "doc_type": "policy",
            },
        },
        {
            "id": "doc-002",
            "score": 0.75,
            "payload": {
                "text": "保修条款：1 年质保",
                "source": "policy_warranty",
                "doc_type": "policy",
            },
        },
    ]

    # mock embedding provider
    mock_embed = MagicMock()
    mock_embed.embed_text.return_value = [0.1] * 1024

    # mock qdrant_search（policy_service → rag/qdrant_impl 内部调）
    # use_hybrid=False 避免 BM25 真实路径干扰（spec §3.4 #1 只测 search）
    with patch(
        "app.rag.qdrant_impl.get_embedding_provider", return_value=mock_embed
    ), patch(
        "app.rag.qdrant_impl.qdrant_search", return_value=mock_hits
    ) as mock_qs:
        ks = QdrantKnowledgeSource()
        results = await ks.search(
            "退货政策", top_k=5, filters={"use_hybrid": False}
        )

    # 断言
    assert len(results) == 2
    assert isinstance(results[0], SearchResult)
    assert results[0].doc_id == "doc-001"
    assert results[0].content == "退货政策：7 天无理由退货"
    assert results[0].score == 0.92
    assert results[0].metadata["source"] == "policy_return"
    assert results[0].metadata["doc_type"] == "policy"
    assert results[1].doc_id == "doc-002"
    assert results[1].score == 0.75


# =============================================================
# 2. test_qdrant_search_with_filters
# =============================================================
@pytest.mark.asyncio
async def test_qdrant_search_with_filters():
    """filters 参数生效（score_threshold + coarse_top_k）"""
    # 让 qdrant.search 接收到的 score_threshold 与 top_k 与 filters 一致
    mock_hits = [
        {"id": "doc-A", "score": 0.88, "payload": {"text": "A", "source": "s_a"}},
    ]

    mock_embed = MagicMock()
    mock_embed.embed_text.return_value = [0.2] * 1024

    with patch(
        "app.rag.qdrant_impl.get_embedding_provider", return_value=mock_embed
    ), patch(
        "app.rag.qdrant_impl.qdrant_search", return_value=mock_hits
    ) as mock_search:
        ks = QdrantKnowledgeSource()
        results = await ks.search(
            "测试",
            top_k=2,
            filters={"score_threshold": 0.5, "coarse_top_k": 8, "use_hybrid": False},
        )

    # 断言：qdrant_search 调用参数
    call_kwargs = mock_search.call_args.kwargs
    assert mock_search.called
    assert call_kwargs["score_threshold"] == 0.5
    assert call_kwargs["top_k"] == 8  # coarse_top_k 覆盖
    assert call_kwargs["collection_name"] == "knowledge_base"  # 默认

    # 返回值正确
    assert len(results) == 1
    assert results[0].doc_id == "doc-A"


# =============================================================
# 3. test_qdrant_get_document_not_found
# =============================================================
@pytest.mark.asyncio
async def test_qdrant_get_document_not_found():
    """doc_id 不存在 → get_document 返 None"""
    # mock qdrant client.retrieve 返空
    mock_client = MagicMock()
    mock_client.retrieve.return_value = []

    with patch("app.rag.qdrant_impl.get_client", return_value=mock_client):
        ks = QdrantKnowledgeSource()
        result = await ks.get_document("not-exist-123")

    assert result is None
    # 验证 retrieve 调用参数正确
    call_kwargs = mock_client.retrieve.call_args.kwargs
    assert call_kwargs["ids"] == ["not-exist-123"]
    assert call_kwargs["with_payload"] is True


# =============================================================
# 4. test_qdrant_upsert_returns_doc_id
# =============================================================
@pytest.mark.asyncio
async def test_qdrant_upsert_returns_doc_id():
    """upsert 协议签名：当前 MVP 抛 NotImplementedError（Sprint 17 仅交付协议）"""
    ks = QdrantKnowledgeSource()
    doc = Document(
        doc_id=None,
        title="新政策",
        content="新政策内容",
        category="policy",
        metadata={"source": "test_policy"},
    )
    # Sprint 17 MVP：upsert 走 services/rag/ingest.py，本类仅占位协议签名
    with pytest.raises(NotImplementedError) as exc_info:
        await ks.upsert(doc)
    assert "upsert" in str(exc_info.value).lower()
    assert "MVP" in str(exc_info.value)


# =============================================================
# 5. test_policy_service_uses_protocol
# =============================================================
def test_policy_service_uses_protocol(monkeypatch):
    """PolicyService.search_policy_via_protocol 通过 _get_knowledge_source 走 Protocol

    验证：mock 替换 _get_knowledge_source 后，PolicyService 调用 mock 实例的 .search()
    """
    from app.services.policy_service import PolicyService

    # 准备 mock KnowledgeSource
    mock_ks = MagicMock()
    mock_ks.search = AsyncMock(
        return_value=[
            SearchResult(
                doc_id="mock-doc-1",
                content="mock 内容",
                score=0.8,
                metadata={"source": "mock_src", "doc_type": "policy"},
            )
        ]
    )

    # 替换 _get_knowledge_source（spec §3.4 #5 mock 替换点）
    monkeypatch.setattr(
        "app.services.policy_service._get_knowledge_source",
        lambda: mock_ks,
    )

    # 调用 PolicyService 新方法
    results = PolicyService.search_policy_via_protocol("测试 query", top_k=3)

    # 断言：mock 被调用 + 参数正确
    assert mock_ks.search.called, f"mock_ks.search was not called, results={results}"
    call_args = mock_ks.search.call_args
    # 支持位置或关键字参数
    if "query" in call_args.kwargs:
        call_query = call_args.kwargs["query"]
        call_top_k = call_args.kwargs.get("top_k", 5)
    elif call_args.args:
        call_query = call_args.args[0]
        call_top_k = call_args.kwargs.get("top_k", call_args.args[1] if len(call_args.args) > 1 else 5)
    else:
        call_query, call_top_k = None, None
    assert call_query == "测试 query"
    assert call_top_k == 3

    # 断言：返回 schema 兼容 search_policy
    assert len(results) == 1
    assert results[0]["text"] == "mock 内容"
    assert results[0]["source"] == "mock_src"
    assert results[0]["score"] == 0.8
    assert results[0]["rerank_score"] is None  # SearchResult 默认无 rerank
    assert results[0]["rrf_score"] is None


# =============================================================
# 6. test_factory_returns_qdrant_impl
# =============================================================
def test_factory_returns_qdrant_impl():
    """factory.get_knowledge_source() → QdrantKnowledgeSource 实例"""
    # 清 lru_cache 单例确保重读
    reset_knowledge_source()

    ks = get_knowledge_source()
    assert isinstance(ks, QdrantKnowledgeSource)
    assert ks.source_type == "qdrant"

    # 单例：第二次拿同一对象
    ks2 = get_knowledge_source()
    assert ks is ks2

    # 清理
    reset_knowledge_source()