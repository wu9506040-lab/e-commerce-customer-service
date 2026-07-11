"""
core.providers — AI 能力 Provider 抽象层（Sprint 1 引入）

按 CLAUDE.md §9.3.3：所有 AI 能力必须抽象封装，业务模块禁止直接调用第三方 AI SDK。

本包包含 3 个 Provider：
- llm:        LLMProvider        (chat / stream_chat)
- embedding:  EmbeddingProvider  (embed_text / embed_texts / get_dim / get_model)
- rerank:     RerankProvider     (rerank / rerank_async)

业务模块通过工厂方法获取 Provider 单例：
    from app.core.providers.llm import get_llm_provider
    llm = get_llm_provider()
    result = llm.chat(messages)

当前唯一实现：Qwen (DashScope OpenAI 兼容)。第二个实现待 V3+。

注：旧路径 `app/core/qwen.py` `app/core/embedding.py` 保留为兼容垫片，
业务模块应改用本包。deprecation 周期至 S4 末删除。
"""