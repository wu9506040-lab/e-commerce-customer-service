"""
core.providers.llm — LLM Provider 公开接口

业务模块统一从此处导入：
    from app.core.providers.llm import get_llm_provider, LLMProvider
"""
from app.core.providers.llm.protocols import LLMProvider
from app.core.providers.llm.qwen_provider import QwenLLMProvider

_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    """获取 LLM Provider 单例（懒加载）。

    当前唯一实现：QwenLLMProvider。
    V3+ 替换为 GPTProvider 时仅需修改本函数内部逻辑（路由配置 / 模型名判断）。
    """
    global _provider
    if _provider is None:
        _provider = QwenLLMProvider()
    return _provider


__all__ = ["LLMProvider", "QwenLLMProvider", "get_llm_provider"]