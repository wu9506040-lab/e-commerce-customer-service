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

# pytest 不走 __main__，env 必须在模块顶部 setdefault
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")

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


# =============================================================
# 5. C1：LLMProvider 扩 tools/tool_choice - Protocol 形状
# =============================================================
class TestLLMProtocolToolFields:
    """C1：Protocol 扩展 tools/tool_choice 后签名仍兼容 + runtime_checkable 行为。"""

    def test_llm_chat_protocol_has_tools_and_tool_choice(self):
        """Protocol.chat 必须声明 tools / tool_choice 参数。"""
        from app.core.providers.llm.protocols import LLMProvider

        sig = inspect.signature(LLMProvider.chat)
        params = list(sig.parameters.keys())
        assert "tools" in params, "chat 协议必须含 tools 参数"
        assert "tool_choice" in params, "chat 协议必须含 tool_choice 参数"

    def test_llm_stream_chat_protocol_has_tools_and_tool_choice(self):
        """Protocol.stream_chat 也声明 tools/tool_choice（保持接口对称）。"""
        from app.core.providers.llm.protocols import LLMProvider

        sig = inspect.signature(LLMProvider.stream_chat)
        params = list(sig.parameters.keys())
        assert "tools" in params
        assert "tool_choice" in params

    def test_llm_protocol_is_runtime_checkable_with_new_args(self):
        """带 tools/tool_choice 的 Fake 仍能被 isinstance(LLMProvider) 识别。"""
        from app.core.providers.llm.protocols import LLMProvider

        class FakeWithTools:
            def chat(self, messages, model=None, temperature=0.7, max_tokens=None,
                     tools=None, tool_choice=None):
                return {"reply": "x", "model": "m", "usage": {}, "tool_calls": None}

            def stream_chat(self, messages, model=None, temperature=0.7, max_tokens=None,
                            tools=None, tool_choice=None):
                yield "chunk"

        assert isinstance(FakeWithTools(), LLMProvider), \
            "扩展参数后仍必须 runtime_checkable"

    def test_qwen_llm_provider_chat_signature_includes_tools(self):
        """QwenLLMProvider.chat 签名必须包含 tools + tool_choice。"""
        from app.core.providers.llm.qwen_provider import QwenLLMProvider

        sig = inspect.signature(QwenLLMProvider.chat)
        params = list(sig.parameters.keys())
        assert "tools" in params
        assert "tool_choice" in params

    def test_qwen_llm_provider_stream_chat_signature_includes_tools(self):
        """QwenLLMProvider.stream_chat 签名必须包含 tools + tool_choice（保持对称）。"""
        from app.core.providers.llm.qwen_provider import QwenLLMProvider

        sig = inspect.signature(QwenLLMProvider.stream_chat)
        params = list(sig.parameters.keys())
        assert "tools" in params
        assert "tool_choice" in params


# =============================================================
# 6. C1：QwenLLMProvider.chat 透传 tools/tool_choice
# =============================================================
class TestQwenProviderToolPassthrough:
    """C1：QwenLLMProvider.chat 必须把 tools/tool_choice 透传给底层 _legacy_qwen.chat。"""

    def test_chat_passes_tools_to_legacy(self):
        """传 tools 时必须透传给 _legacy_qwen.chat 的 tools 关键字参数。"""
        from app.core.providers.llm.qwen_provider import QwenLLMProvider

        provider = QwenLLMProvider()
        fake_tools = [
            {"type": "function", "function": {"name": "lookup_order",
                                              "description": "x", "parameters": {}}}
        ]
        fake_reply = {"reply": "ok", "model": "m", "usage": {}, "tool_calls": None}

        with patch("app.core.providers.llm.qwen_provider._legacy_qwen.chat",
                   return_value=fake_reply) as mock_legacy:
            result = provider.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=fake_tools,
                tool_choice="auto",
            )

        assert result == fake_reply
        mock_legacy.assert_called_once()
        call_kwargs = mock_legacy.call_args.kwargs
        assert call_kwargs["tools"] == fake_tools
        assert call_kwargs["tool_choice"] == "auto"

    def test_chat_without_tools_passes_none(self):
        """不传 tools 时必须显式传 None 给 _legacy_qwen.chat（避免下游 NoneType 错）。"""
        from app.core.providers.llm.qwen_provider import QwenLLMProvider

        provider = QwenLLMProvider()
        fake_reply = {"reply": "ok", "model": "m", "usage": {}, "tool_calls": None}

        with patch("app.core.providers.llm.qwen_provider._legacy_qwen.chat",
                   return_value=fake_reply) as mock_legacy:
            result = provider.chat(messages=[{"role": "user", "content": "hi"}])

        assert result == fake_reply
        call_kwargs = mock_legacy.call_args.kwargs
        assert call_kwargs["tools"] is None
        assert call_kwargs["tool_choice"] is None


# =============================================================
# 7. C1：app.core.qwen.chat 透传 + 抽 tool_calls
# =============================================================
class TestQwenLegacyToolCallsExtraction:
    """C1：app.core.qwen.chat 把 tools 加进 kwargs；返回 dict 抽 tool_calls 字段。"""

    def _make_mock_response(self, content="hello", tool_calls=None, usage=None):
        """构造 mock OpenAI response 对象。"""
        usage = usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        msg = MagicMock()
        msg.content = content
        msg.tool_calls = tool_calls

        choice = MagicMock()
        choice.message = msg

        response = MagicMock()
        response.choices = [choice]
        response.usage.prompt_tokens = usage["prompt_tokens"]
        response.usage.completion_tokens = usage["completion_tokens"]
        response.usage.total_tokens = usage["total_tokens"]
        return response

    def _make_fake_tool_call(self, call_id, func_name, args_json):
        """构造 mock OpenAI tool_call。"""
        tc = MagicMock()
        tc.id = call_id
        tc.type = "function"
        tc.function.name = func_name
        tc.function.arguments = args_json
        return tc

    def test_legacy_chat_passes_tools_in_kwargs(self):
        """app.core.qwen.chat 必须把 tools 加进 client.create kwargs。"""
        from app.core import qwen as legacy_qwen

        fake_tools = [{"type": "function", "function": {"name": "f", "description": "d",
                                                       "parameters": {}}}]
        mock_response = self._make_mock_response()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch.object(legacy_qwen, "get_client", return_value=mock_client), \
             patch.object(legacy_qwen, "_qwen_breaker") as mock_breaker:
            mock_breaker.call.return_value = mock_response
            legacy_qwen.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=fake_tools,
                tool_choice="auto",
            )

        mock_breaker.call.assert_called_once()
        passed_kwargs = mock_breaker.call.call_args.kwargs
        assert passed_kwargs["tools"] == fake_tools
        assert passed_kwargs["tool_choice"] == "auto"

    def test_legacy_chat_no_tools_omits_kwarg(self):
        """tools=None 时，client.create kwargs 中不应包含 tools（避免 None 触发 SDK 警告）。"""
        from app.core import qwen as legacy_qwen

        mock_response = self._make_mock_response()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch.object(legacy_qwen, "get_client", return_value=mock_client), \
             patch.object(legacy_qwen, "_qwen_breaker") as mock_breaker:
            mock_breaker.call.return_value = mock_response
            legacy_qwen.chat(messages=[{"role": "user", "content": "hi"}])

        passed_kwargs = mock_breaker.call.call_args.kwargs
        assert "tools" not in passed_kwargs, \
            "tools=None 时不应传给 OpenAI client（保留上游行为）"
        assert "tool_choice" not in passed_kwargs

    def test_legacy_chat_extracts_tool_calls_into_dict(self):
        """响应含 tool_calls 时，返回 dict 必须含结构化 tool_calls list。"""
        from app.core import qwen as legacy_qwen

        fake_tc = self._make_fake_tool_call("call_123", "lookup_order", '{"order_id": 1}')
        mock_response = self._make_mock_response(content=None, tool_calls=[fake_tc])
        mock_client = MagicMock()

        with patch.object(legacy_qwen, "get_client", return_value=mock_client), \
             patch.object(legacy_qwen, "_qwen_breaker") as mock_breaker:
            mock_breaker.call.return_value = mock_response
            result = legacy_qwen.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "lookup_order"}}],
            )

        assert result["tool_calls"] is not None
        assert len(result["tool_calls"]) == 1
        tc = result["tool_calls"][0]
        assert tc["id"] == "call_123"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "lookup_order"
        assert tc["function"]["arguments"] == '{"order_id": 1}'

    def test_legacy_chat_no_tool_calls_returns_none(self):
        """响应无 tool_calls 时（普通 chat），返回 dict 的 tool_calls 必须为 None。"""
        from app.core import qwen as legacy_qwen

        mock_response = self._make_mock_response(content="just a normal reply")
        mock_client = MagicMock()

        with patch.object(legacy_qwen, "get_client", return_value=mock_client), \
             patch.object(legacy_qwen, "_qwen_breaker") as mock_breaker:
            mock_breaker.call.return_value = mock_response
            result = legacy_qwen.chat(messages=[{"role": "user", "content": "hi"}])

        assert result["reply"] == "just a normal reply"
        assert result["tool_calls"] is None