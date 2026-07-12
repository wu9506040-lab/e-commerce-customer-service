"""
Stream Dispatcher（Sprint 3 拆分自 synthesizer.py）

职责：LLM 流式输出 + 简单文本流的统一封装 + 滑动窗口关键词 fallback。
- 含 _LLM_SEMAPHORE（模块级并发限流闸）
- 含 _stream_llm（走 LLM Provider stream_chat）
- 含 _stream_simple（不走 LLM，直接 yield）
- 含 _search_by_keyword_window（product handler 用，滑动窗口抽 2-3 字实词）
- 含 SYSTEM_PROMPT_BASE（系统 Prompt 常量；从 prompt_assembler 转一道封装）

边界：仅做流式输出；不构造 prompt（委托 prompt_assembler）；不做 intent 分派（委托 orchestrator）。
"""
import logging
import re
import threading
from typing import Any, Generator

from app.core.providers.llm import get_llm_provider
from app.services.chat.prompt_assembler import SYSTEM_PROMPT_BASE
from app.services.metrics import metrics
from app.tools.product_tool import ProductTool

logger = logging.getLogger(__name__)

# §9 并发控制：P1 压测发现 50 并发直接打 LLM 触发 DashScope 限流（429）
# 用 semaphore 限流到 10 路并发，超出请求排队等待
# 实测 DashScope qwen-plus 默认 ~60 QPM，10 并发是安全水位
_LLM_SEMAPHORE = threading.Semaphore(10)


def stream_llm(user_prompt: str) -> Generator[tuple[str, Any], None, None]:
    """单 LLM 流式调用 + done 事件（§9 并发限流 semaphore=10）

    P1：max_tokens=256 压输出长度，与 SYSTEM_PROMPT_BASE "200 字以内" 硬约束对齐
    （512 token ≈ 1500 中文字符，会让 LLM 写长文超出 prompt 字数限制）
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_BASE},
        {"role": "user", "content": user_prompt},
    ]
    full_answer = ""
    # semaphore 包住整个流式调用：>10 并发时排队，超出请求首 token 延迟增大但不会 429
    with _LLM_SEMAPHORE:
        for chunk in get_llm_provider().stream_chat(messages, temperature=0.3, max_tokens=256):
            full_answer += chunk
            yield ("token", chunk)
    # M8：粗估 token 数（中文 ~1 char ≈ 1.5 token；这里简化为 char 数）
    metrics.record_answer_tokens(len(full_answer))
    yield ("done", {"answer": full_answer})


def search_by_keyword_window(query: str, limit: int = 5) -> list[dict]:
    """用滑动窗口（2-3 字）抽 query 里的实词，逐个调 ProductTool.search_by_keyword，
    命中即返回。最坏情况下 N 次调用（N = 候选数）— 接受（小数据集，前缀检查会快速失败）
    """
    seen = set()
    candidates = []
    for size in (2, 3):
        for i in range(len(query) - size + 1):
            c = query[i:i + size]
            # 只保留纯中文字段
            if re.fullmatch(r"[\u4e00-\u9fff]+", c) and c not in seen:
                seen.add(c)
                candidates.append(c)
    # 按出现顺序（自然语言里关键词偏后）；倒序先查"尾巴词"
    for kw in reversed(candidates):
        ps = ProductTool.search_by_keyword(kw, limit=limit)
        if ps:
            logger.info(f"product keyword window 命中: kw='{kw}' → {len(ps)} 条")
            return ps
    return []


def stream_simple(text: str) -> Generator[tuple[str, Any], None, None]:
    """简单文本直接 yield（不走 LLM）— 仍按 token + done 协议，chat.py 通用累加逻辑可工作"""
    yield ("token", text)
    metrics.record_answer_tokens(len(text))  # M8
    yield ("done", {"answer": text})
