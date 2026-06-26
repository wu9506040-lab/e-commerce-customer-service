"""
RAG Pipeline - 检索增强生成的编排层

按 §6 规则：
- services/ 编排层，可调 core/ 和 clients/
- 不写 API（HTTP 路由在 api/）
- 不写 chat 接口（chat 在 api/chat.py）
- 不做数据库模型（DB 模型在 models/）
- 不做 Agent（Agent 编排单独模块）

数据流：
    query → embed → qdrant.search → context 组装 → prompt → qwen LLM → answer

§14 起：提供 run_stream() 流式版本（供 /chat SSE 输出）
"""
import logging
from typing import Dict, List, Any, Optional, Generator, Tuple

from app.core.embedding import embed_text
from app.core.qwen import chat as qwen_chat, stream_chat as qwen_stream_chat
from app.clients.qdrant import search as qdrant_search

logger = logging.getLogger(__name__)

# =============================================================
# 配置
# =============================================================
TOP_K = 5
# prompt 模板：检索内容 + 用户问题
PROMPT_TEMPLATE = (
    "基于以下内容回答问题：\n"
    "{context}\n"
    "{history_block}"
    "问题：{query}"
)
# system prompt：约束 LLM 只基于知识库回答
SYSTEM_PROMPT = (
    "你是一个专业的客服助手。"
    "请仅根据提供的【参考资料】回答用户问题。"
    "如果参考资料不足以回答问题，请直接回答「我不知道」。"
    "回答要简洁、准确，不要编造信息。"
    "如果有【对话历史】，请结合历史上下文理解用户当前问题。"
)
HISTORY_BLOCK_TEMPLATE = (
    "\n对话历史：\n{turns}\n"
)


# =============================================================
# 内部辅助
# =============================================================
def _format_context(contexts: List[str]) -> str:
    """
    把检索到的 chunks 拼接成 context 块
    用 [1]/[2]/[3] 编号，方便 LLM 引用
    """
    blocks = []
    for i, c in enumerate(contexts, start=1):
        c = (c or "").strip()
        if not c:
            continue
        blocks.append(f"[{i}] {c}")
    return "\n\n".join(blocks)


def _format_history(history: Optional[List[Dict[str, Any]]]) -> str:
    """
    把历史消息格式化成「对话历史」段
    空列表返回空字符串，模板里不出现该段

    输入格式：[{"role": "user"|"assistant", "content": "..."}]
    """
    if not history:
        return ""

    lines = []
    for msg in history:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            lines.append(f"用户：{content}")
        elif role == "assistant":
            lines.append(f"助手：{content}")
        # 忽略其他 role

    if not lines:
        return ""
    return HISTORY_BLOCK_TEMPLATE.format(turns="\n".join(lines))


def _extract_text(payload: Dict[str, Any]) -> str:
    """
    从 Qdrant payload 中提取文本字段
    约定 payload 结构：{"text": "...", "source": "..."}
    """
    if not payload:
        return ""
    return payload.get("text") or payload.get("content") or ""


# =============================================================
# 主入口（流式版本，§14）
# =============================================================
def run_stream(
    query: str,
    top_k: int = TOP_K,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Generator[Tuple[str, Any], None, None]:
    """
    RAG 流式版本（供 /chat SSE 输出）

    Args:
        query: 用户问题
        top_k: 检索 top-k
        history: 多轮对话历史

    Yields:
        ("meta", {"contexts": [...], "scores": [...]})
            — 检索结果，先于 token 输出（前端可显示来源）
        ("token", str)
            — LLM 文本片段（可能多次）
        ("done", {"answer": str})
            — 流结束，answer 为拼接完整文本（供调用方 write-through）

    Raises:
        ValueError: query 为空
    """
    if not query or not query.strip():
        raise ValueError("run_stream: query 不能为空")

    query = query.strip()
    logger.info(
        f"rag stream start: query='{query}', top_k={top_k}, "
        f"history_len={len(history) if history else 0}"
    )

    # 1. embed + search（同步，快，先拿到 contexts）
    query_vec = embed_text(query)
    hits = qdrant_search(query_vec, top_k=top_k)
    contexts = [_extract_text(h.get("payload") or {}) for h in hits]
    scores = [float(h.get("score") or 0.0) for h in hits]

    yield ("meta", {"contexts": contexts, "scores": scores})
    logger.info(f"rag stream retrieve: hits={len(hits)}")

    # 2. 组装 prompt（context + 可选 history + 当前问题）
    context_block = _format_context(contexts)
    history_block = _format_history(history)
    user_prompt = PROMPT_TEMPLATE.format(
        context=context_block,
        history_block=history_block,
        query=query,
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # 3. 流式 LLM
    full_answer = ""
    for chunk in qwen_stream_chat(messages, temperature=0.3):
        full_answer += chunk
        yield ("token", chunk)

    # 4. 流结束
    yield ("done", {"answer": full_answer})

    logger.info(
        f"rag stream done: answer_len={len(full_answer)}, "
        f"contexts={len(contexts)}, scores={scores}"
    )