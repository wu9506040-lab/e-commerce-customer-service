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
from typing import Any, Generator, Optional

from app.core import deadline as _deadline
from app.core.config import settings
from app.core.providers.llm import get_llm_provider
from app.core.qwen import StreamTruncatedError
from app.services.chat.prompt_assembler import SYSTEM_PROMPT_BASE
from app.services.metrics import metrics
from app.tools.product_tool import ProductTool

logger = logging.getLogger(__name__)

# §9 并发控制：P1 压测发现 50 并发直接打 LLM 触发 DashScope 限流（429）
# 用 semaphore 限流到 10 路并发，超出请求排队等待
# 实测 DashScope qwen-plus 默认 ~60 QPM，10 并发是安全水位
# V14（3.3）：闸容量从硬编码进 Settings，且排队等待上限=min(settings.ACQUIRE_TIMEOUT, 预算剩余)——
# 排队不应无限等，预算里也明文禁止"排队吃预算"
_LLM_SEMAPHORE = threading.Semaphore(settings.LLM_SEMAPHORE_SIZE)


def _build_system_content(context_block: Optional[str]) -> str:
    """组装 system message content — V16.3.4 把渠道上下文 promote 到 system。

    当 caller 传 context_block（闲鱼/微信/抖音 等渠道 daemon 拼的 买家画像+商品话术），
    把它作为"系统级结构化事实"拼到 SYSTEM_PROMPT_BASE 之后，让 LLM 真把它当参考
    资料，避免被原 SYSTEM 措辞"严禁编造参考资料以外"压垮。

    不传则等同于原 SYSTEM_PROMPT_BASE（向后兼容——前端/老 daemon 无影响）。
    """
    if not context_block:
        return SYSTEM_PROMPT_BASE
    return (
        f"{SYSTEM_PROMPT_BASE}\n\n"
        f"【结构化事实-渠道上下文（最高优先级）】\n"
        f"{context_block}"
    )


def stream_llm(
    user_prompt: str,
    context_block: Optional[str] = None,
    system_prompt: Optional[str] = None,  # V18-P0：caller 全量 system prompt（优先级最高）
    history: Optional[list] = None,  # V18-P0：多轮历史（xianyu 快速通道追问用）
    max_tokens: int = 256,  # V21：列表型商品清单需要更长输出（默认 256 压长度）
) -> Generator[tuple[str, Any], None, None]:
    """单 LLM 流式调用 + done 事件（§9 并发限流 semaphore=10 + 3.3 预算）

    P1：max_tokens=256 压输出长度，与 SYSTEM_PROMPT_BASE "200 字以内" 硬约束对齐
    （512 token ≈ 1500 中文字符，会让 LLM 写长文超出 prompt 字数限制）

    V16.3.4：增 context_block 参数（daemon/caller 渠道上下文）—— **架构修法**：
    把 context_block 拼到 system message 头部（在 SYSTEM_PROMPT_BASE 之后），让 LLM
    视为与 SYSTEM_PROMPT_BASE 同级"参考资料"。先前 V16.3.4 改 YAML 措辞验证未
    生效（context_block 在 user message 的【当前场景】段被 LLM 视为"场景补充"
    而非"事实"——被 SYSTEM 原"严禁编造参考资料外"规则压垮），故改此处从 stream
    层 promote。

    V18-P0：增 system_prompt（caller 全量 system，直接替代默认 system）+
    history（多轮历史插在 system 与 user 之间）—— xianyu 快速通道用。
    """
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_prompt})
    else:
        messages = [
            {"role": "system", "content": _build_system_content(context_block)},
            {"role": "user", "content": user_prompt},
        ]
    full_answer = ""
    # semaphore + 排队上限 + 预算裁剪（3.3）：>10 并发排队等待，超时/超预算快速失败走降级
    truncated = False
    acquire_budget = _deadline.clamp_timeout(settings.LLM_SEMAPHORE_ACQUIRE_TIMEOUT)
    acquired = _LLM_SEMAPHORE.acquire(timeout=acquire_budget)
    if not acquired:
        metrics.record_sem_wait_timeout()
        raise _deadline.DeadlineExceededError(
            f"LLM concurrency gate queueing timed out ({acquire_budget:.1f}s)",
            stage="llm.semaphore",
        )
    try:
        try:
            for chunk in get_llm_provider().stream_chat(messages, temperature=0.3, max_tokens=max_tokens):
                full_answer += chunk
                yield ("token", chunk)
        except StreamTruncatedError as e:
            # V13（1.2）：中途断连=残答——正常收尾但 done 事件打 truncated 标记，
            # 由 chat.py 做缓存隔离（残答禁入语义缓存，否则会反复供应半截答案）
            truncated = True
            logger.warning(f"stream_llm 收到截断信号: {e}")
    finally:
        _LLM_SEMAPHORE.release()
    # M8：粗估 token 数（中文 ~1 char ≈ 1.5 token；这里简化为 char 数）
    metrics.record_answer_tokens(len(full_answer))
    metrics.record_llm_stream(truncated=truncated)
    # V16.5 全链路日志：LLM_RESPONSE（不动业务：仅在 done yield 前打 1 行）
    try:
        from app.core.runtime_log import log_event as _relog
        _relog(
            "LLM_RESPONSE", "INFO", "-", "-",
            f"stream done answer_len={len(full_answer)} truncated={truncated}",
            answer_len=len(full_answer),
            truncated=truncated,
            model="qwen-max",
            final_answer=full_answer[:500],
        )
    except Exception:
        pass
    yield ("done", {"answer": full_answer, "truncated": truncated})


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
