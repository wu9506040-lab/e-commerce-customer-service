"""
QwenLLMProvider — Qwen (DashScope OpenAI 兼容) LLM 实现

内部委托给 `app.core.qwen` 模块级函数（保留 retry + 指数退避 + 抖动 + 断路器）。
不改业务逻辑，仅做方法签名适配 Protocol。

说明：`app.core.qwen` 是 Provider 的内部 DashScope 客户端实现，
业务模块禁止直接 import，只能通过本 Provider 调用。

C1：Protocol 扩展 Function Calling 后，本 Provider 透传 tools/tool_choice 给底层，
并在 chat 返回中提取 response.choices[0].message.tool_calls 为结构化 list[dict]。
"""
from typing import List, Dict, Generator, Optional

from app.core import qwen as _legacy_qwen


class QwenLLMProvider:
    """Qwen LLM Provider 实现。

    复用 `app.core.qwen` 模块级 chat / stream_chat（含重试 + 断路器）。
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
        return _legacy_qwen.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
    ) -> Generator[str, None, None]:
        # C1：保留接口一致性，但当前不真正透传 tools 给 DashScope 流式端点。
        # 流式 FC 需要 chunk 状态机拼接，C2 不会用流式 FC。
        return _legacy_qwen.stream_chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )