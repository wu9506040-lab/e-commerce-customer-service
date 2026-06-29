"""
Query Rewriter - 多轮对话的指代补全（M12）

按 §6 规则：services/ 编排层，可调 core/qwen.py
不动 api/ / intent_service / policy_service

三层防浪费：
- L0 规则检测：含指代词才进入下一步（零成本）
- L1 history 检查：无 history 跳过（零成本）
- L2 LLM 改写：单次 chat 调用补全指代（条件触发）

降级：任意环节失败 → 返原 query（不阻塞业务）

设计取舍：
- 只做指代补全，不做 query 扩展/Multi-Query/HyDE
  （YAGNI：先解决最高频痛点，验证有效再做其他）
- temperature=0 + 短 prompt + 原 query 必带 → 改坏概率低
- 仅在 product_query / policy_query 路径有效（其他路径不读 query 检索）
- intent 分类前调用：避免「它」「这个」被识别成无效 query
"""
import logging
import re
from typing import Dict, List, Optional, Tuple

from app.core.qwen import chat as qwen_chat
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

# L0：指代词清单（覆盖电商场景常见代词）
# 来源：电商客服多轮对话高频观察 + 中文指代词表精简
COREFERENCE_PATTERNS = re.compile(
    r"它|他们|这个|那个|这些|那些|刚才|之前|上面|下面|"
    r"那款|这款|这种|那种|前一个|后一个|前者|后者|这里|那里"
)

# LLM 改写 prompt（精简版，控制 token）
REWRITE_SYSTEM_PROMPT = (
    "你是电商客服 query 改写员。"
    "任务：把用户问题里的指代词补全为具体实体（商品名/SKU/订单号/颜色等）。"
    "规则："
    "1. 仅做指代补全，不改写意图、不补全新信息、不回答问题"
    "2. 历史里没提到的指代，原样保留"
    "3. 保留原 query 的语气和长度"
    "4. 只输出改写后的 query，不要任何解释"
)

REWRITE_USER_TEMPLATE = (
    "对话历史：\n{history}\n\n"
    "当前问题：{query}\n\n"
    "改写后："
)

# 截短 history：避免 prompt 过长（只取最近 4 条）
MAX_HISTORY_TURNS = 4
# history 单条最长字符数
MAX_HISTORY_MSG_LEN = 100
# 改写结果长度上限：原 query * 3 + 50（防 LLM 输出失控）
MAX_REWRITE_RATIO = 3
MAX_REWRITE_EXTRA = 50


def _has_coreference(query: str) -> bool:
    """L0 规则检测：query 是否含指代词"""
    return bool(COREFERENCE_PATTERNS.search(query))


def _format_history_snippet(history: List[Dict]) -> str:
    """格式化 history 为简短片段（供 LLM 看）"""
    if not history:
        return ""
    # 只取最近 MAX_HISTORY_TURNS 条
    recent = history[-MAX_HISTORY_TURNS:]
    lines = []
    for msg in recent:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        # 截断单条消息
        if len(content) > MAX_HISTORY_MSG_LEN:
            content = content[:MAX_HISTORY_MSG_LEN] + "..."
        prefix = "用户" if role == "user" else "客服" if role == "assistant" else str(role)
        lines.append(f"[{prefix}] {content}")
    return "\n".join(lines)


def rewrite_query(
    query: str, history: Optional[List[Dict]] = None
) -> Tuple[str, bool]:
    """
    指代补全入口

    Args:
        query: 用户当前问题
        history: 多轮对话历史 [{"role", "content"}]

    Returns:
        (rewritten_query, was_rewritten):
        - 无需改写（无指代词/无 history/LLM 失败）→ (query, False)
        - 已改写 → (改写后, True)
    """
    if not query or not query.strip():
        return query, False

    query = query.strip()

    # L0：规则检测
    if not _has_coreference(query):
        metrics.inc_rewrite("skipped_no_coref")
        logger.debug(f"rewrite skip (no coreference): '{query[:30]}...'")
        return query, False

    # L1：history 检查
    if not history:
        metrics.inc_rewrite("skipped_no_history")
        logger.debug(f"rewrite skip (no history): '{query[:30]}...'")
        return query, False

    # L2：LLM 改写
    history_str = _format_history_snippet(history)
    user_prompt = REWRITE_USER_TEMPLATE.format(history=history_str, query=query)

    try:
        result = qwen_chat(
            [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=80,
        )
        rewritten = (result.get("reply") or "").strip()
        # 清理：去掉可能的引号、句号
        rewritten = rewritten.strip('"\'""''。').strip()
        if not rewritten:
            logger.warning(
                f"rewrite 返回空，fallback: query='{query[:30]}...'"
            )
            metrics.inc_rewrite("error_empty")
            return query, False
        # 防护：改写结果过长 → 降级（防 LLM 失控输出）
        max_len = len(query) * MAX_REWRITE_RATIO + MAX_REWRITE_EXTRA
        if len(rewritten) > max_len:
            logger.warning(
                f"rewrite 结果过长，fallback: orig='{query[:30]}...' "
                f"rewrite_len={len(rewritten)} max={max_len}"
            )
            metrics.inc_rewrite("error_too_long")
            return query, False
        metrics.inc_rewrite("rewritten")
        logger.info(
            f"rewrite done: '{query[:30]}...' -> '{rewritten[:50]}...' "
            f"(orig_len={len(query)}, rewrite_len={len(rewritten)})"
        )
        return rewritten, True
    except Exception as e:
        logger.warning(
            f"rewrite LLM 异常，fallback: query='{query[:30]}...' "
            f"err={type(e).__name__}: {str(e)[:100]}"
        )
        metrics.inc_rewrite("error_llm")
        return query, False