"""
QwenLLMProvider — Qwen (DashScope OpenAI 兼容) LLM 实现

内部委托给 `app.core.qwen` 模块级函数（保留 retry + 指数退避 + 抖动 + 断路器）。
不改业务逻辑，仅做方法签名适配 Protocol。
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
    ) -> Dict:
        return _legacy_qwen.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        return _legacy_qwen.stream_chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )