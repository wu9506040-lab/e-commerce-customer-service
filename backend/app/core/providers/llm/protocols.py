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

    Function Calling 支持（C1 新增）：
        - chat(messages, tools=[...], tool_choice="auto")：同步工具调用
          返回 dict 新增 tool_calls 字段（list[dict]），结构：
              [{"id": "call_xxx", "type": "function",
                "function": {"name": "...", "arguments": "{...json...}"}}]
        - stream_chat(...)：C1 仅在 Protocol 层加参数透传；
          **当前不真正实现流式 FC**（保持行为和现状一致）。
          C2 Agent FC 框架用 chat() 实现工具分发，避免流式 FC 的状态机复杂度。
    """

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
    ) -> Dict:
        """同步 chat 调用（支持 Function Calling）。

        Args:
            messages: OpenAI 风格消息列表 [{"role", "content"}, ...]
            model: 模型名（None 用 settings 默认）
            temperature: 0-2
            max_tokens: 输出 token 上限（None = 模型默认）
            tools: OpenAI 风格工具定义列表（None = 不传 tools）。格式:
                [{"type": "function", "function": {"name", "description", "parameters"}}]
            tool_choice: "auto" / "none" / {"type": "function", "function": {"name": "..."}}
                None = 不传（由模型默认行为决定）

        Returns:
            {
                "reply": str,
                "model": str,
                "usage": {"prompt_tokens", "completion_tokens", "total_tokens"},
                "tool_calls": Optional[List[Dict]]  # 模型决定调用工具时非空
            }

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
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """流式 chat 调用。

        仅对 client.create() 阶段做 retry + 断路器。流式中途断连仅 log，不重试。

        Note:
            tools/tool_choice 参数保留接口一致性，但**当前 QwenLLMProvider 不真正透传
            给 DashScope 流式端点**。Agent FC 场景请用 chat()。

        Yields:
            文本片段（可能为空字符串，已过滤）
        """
        ...