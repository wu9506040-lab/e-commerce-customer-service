"""
Sprint 1: AI Provider Layer 抽象 - Protocol + Factory 单测

覆盖：
1. 3 个 Protocol 都是 runtime_checkable（业务模块可以 isinstance 检查）
2. 3 个工厂返回单例（get_llm/embedding/rerank_provider）
3. Provider 实现的方法签名与 Protocol 一致（不依赖外部服务）

设计原则：所有测试**不调真实 LLM / Embedding / Qdrant**，只验证：
- Protocol 形状（鸭子类型）
- 单例模式
- 包装函数的参数透传

依赖 Sprint 1 约束：不改 API / 不改业务逻辑 / 不改数据库 / 不迁目录。
"""
import os
import sys
import inspect
from unittest.mock import patch, MagicMock

import pytest

# 让模块能找到 app 包（与项目其他测试一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================
# 1. Protocol runtime_checkable 检查
# =============================================================
class TestProtocolsRuntimeCheckable:
    """验证 Protocol 是 runtime_checkable（业务模块可做 isinstance 检查）。"""

    def test_llm_protocol_is_runtime_checkable(self):
        from app.core.providers.llm.protocols import LLMProvider

        # 必须能 isinstance 检查（@runtime_checkable 标记）
        class Fake:
            def chat(self, messages, model=None, temperature=0.7, max_tokens=None):
                return {"reply": "x", "model": "m", "usage": {}}

            def stream_chat(self, messages, model=None, temperature=0.7, max_tokens=None):
                yield "chunk"

        assert isinstance(Fake(), LLMProvider), "LLMProvider 必须 runtime_checkable"

    def test_embedding_protocol_is_runtime_checkable(self):
        from app.core.providers.embedding.protocols import EmbeddingProvider

        class Fake:
            def embed_text(self, text):
                return [0.0] * 1024

            def embed_texts(self, texts):
                return [[0.0] * 1024] * len(texts)

            def get_dim(self):
                return 1024

            def get_model(self):
                return "fake-model"

        assert isinstance(Fake(), EmbeddingProvider), "EmbeddingProvider 必须 runtime_checkable"

    def test_rerank_protocol_is_runtime_checkable(self):
        from app.core.providers.rerank.protocols import RerankProvider

        class Fake:
            def rerank(self, query, candidates, top_n=None):
                return []

            async def rerank_async(self, query, candidates, top_n=None):
                return []

        assert isinstance(Fake(), RerankProvider), "RerankProvider 必须 runtime_checkable"

    def test_embedding_error_inherits_exception(self):
        """EmbeddingError 必须能被业务层 try/except（向后兼容）。"""
        from app.core.providers.embedding.protocols import EmbeddingError

        assert issubclass(EmbeddingError, Exception)
        with pytest.raises(EmbeddingError):
            raise EmbeddingError("test")


# =============================================================
# 2. Factory 单例检查
# =============================================================
class TestFactorySingletons:
    """验证 3 个 get_xxx_provider() 返回单例。"""

    def test_get_llm_provider_singleton(self):
        from app.core.providers.llm import get_llm_provider
        from app.core.providers.llm.qwen_provider import QwenLLMProvider

        a = get_llm_provider()
        b = get_llm_provider()
        assert a is b, "get_llm_provider 必须返回单例"
        assert isinstance(a, QwenLLMProvider)

    def test_get_embedding_provider_singleton(self):
        from app.core.providers.embedding import get_embedding_provider
        from app.core.providers.embedding.qwen_provider import QwenEmbeddingProvider

        a = get_embedding_provider()
        b = get_embedding_provider()
        assert a is b, "get_embedding_provider 必须返回单例"
        assert isinstance(a, QwenEmbeddingProvider)

    def test_get_rerank_provider_singleton(self):
        from app.core.providers.rerank import get_rerank_provider
        from app.core.providers.rerank.qwen_provider import QwenRerankProvider

        a = get_rerank_provider()
        b = get_rerank_provider()
        assert a is b, "get_rerank_provider 必须返回单例"
        assert isinstance(a, QwenRerankProvider)


# =============================================================
# 3. Provider 方法签名与 Protocol 一致
# =============================================================
class TestProviderSignatures:
    """验证 Provider 实现的方法签名（含参数名 / 类型注解）与 Protocol 兼容。"""

    def test_qwen_llm_provider_chat_signature(self):
        """chat() 必须接受 Protocol 声明的 4 个参数（messages/model/temperature/max_tokens）。"""
        from app.core.providers.llm.qwen_provider import QwenLLMProvider

        sig = inspect.signature(QwenLLMProvider.chat)
        params = list(sig.parameters.keys())
        # self + 4 个参数
        assert "messages" in params
        assert "model" in params
        assert "temperature" in params
        assert "max_tokens" in params

    def test_qwen_llm_provider_stream_chat_signature(self):
        """stream_chat() 签名同 chat。"""
        from app.core.providers.llm.qwen_provider import QwenLLMProvider

        sig = inspect.signature(QwenLLMProvider.stream_chat)
        params = list(sig.parameters.keys())
        assert "messages" in params
        assert "model" in params
        assert "temperature" in params
        assert "max_tokens" in params

    def test_qwen_embedding_provider_signature(self):
        """QwenEmbeddingProvider 必须实现 4 个 Protocol 方法。"""
        from app.core.providers.embedding.qwen_provider import QwenEmbeddingProvider

        assert callable(getattr(QwenEmbeddingProvider, "embed_text", None))
        assert callable(getattr(QwenEmbeddingProvider, "embed_texts", None))
        assert callable(getattr(QwenEmbeddingProvider, "get_dim", None))
        assert callable(getattr(QwenEmbeddingProvider, "get_model", None))

    def test_qwen_rerank_provider_signature(self):
        """QwenRerankProvider 必须实现 2 个 Protocol 方法（同步 + 异步）。"""
        from app.core.providers.rerank.qwen_provider import QwenRerankProvider

        assert callable(getattr(QwenRerankProvider, "rerank", None))
        assert callable(getattr(QwenRerankProvider, "rerank_async", None))


# =============================================================
# 4. QwenEmbeddingProvider 委托给 legacy（业务层不变即可工作）
# =============================================================
class TestProviderDelegation:
    """验证 Provider 内部委托给 legacy 模块（composition over inheritance）。"""

    def test_embedding_provider_delegates_to_legacy(self):
        """QwenEmbeddingProvider.embed_text 必须委托给 app.core.embedding.embed_text。"""
        from app.core.providers.embedding.qwen_provider import QwenEmbeddingProvider

        provider = QwenEmbeddingProvider()
        fake_vec = [0.1] * 1024

        with patch("app.core.providers.embedding.qwen_provider._legacy_embedding.embed_text",
                   return_value=fake_vec) as mock_legacy:
            result = provider.embed_text("hello")
            assert result == fake_vec
            mock_legacy.assert_called_once_with("hello")

    def test_embedding_provider_dim_matches_legacy(self):
        """get_dim() 必须与 legacy EMBEDDING_DIM 一致（业务依赖此值配置 Qdrant）。"""
        from app.core.providers.embedding.qwen_provider import QwenEmbeddingProvider

        provider = QwenEmbeddingProvider()
        assert provider.get_dim() == 1024
        assert provider.get_model() == "text-embedding-v3"

    def test_rerank_provider_empty_candidates_short_circuit(self):
        """空 candidates 必须立即返回，不调 LLM（性能契约）。"""
        from app.core.providers.rerank.qwen_provider import QwenRerankProvider

        provider = QwenRerankProvider()
        with patch("app.core.providers.rerank.qwen_provider._legacy_qwen.chat") as mock_chat:
            result = provider.rerank("query", [])
            assert result == []
            mock_chat.assert_not_called()