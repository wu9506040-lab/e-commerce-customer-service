"""
LLMProvider Protocol — 大模型调用抽象

按 CLAUDE.md §9.3.3：业务模块禁止直接调用第三方 LLM SDK。
"""
from typing import Protocol, List, Dict, Generator, Optional, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 能力抽象。

    业务模块通过 `get_llm_provider()` 获取实例，调用 chat / stream_chat。
    当前唯一实现：QwenLLMProvider（基于 DashScope OpenAI 兼容协议）。
    """

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Dict:
        """同步 chat 调用。

        Args:
            messages: OpenAI 风格消息列表 [{"role", "content"}, ...]
            model: 模型名（None 用 settings 默认）
            temperature: 0-2
            max_tokens: 输出 token 上限（None = 模型默认）

        Returns:
            {"reply": str, "model": str, "usage": {"prompt_tokens", "completion_tokens", "total_tokens"}}

        Raises:
            BadRequestError / AuthenticationError / PermissionDeniedError: 业务错（不重试）
            CircuitOpenError: 断路器开路（上游应降级到兜底）
            Exception: 重试耗尽后透传最后一次异常
        """
        ...

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        """流式 chat 调用。

        仅对 client.create() 阶段做 retry + 断路器。流式中途断连仅 log，不重试。

        Yields:
            文本片段（可能为空字符串，已过滤）
        """
        ...