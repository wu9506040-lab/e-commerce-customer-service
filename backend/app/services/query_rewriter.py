"""
Query Rewriter - 多轮对话的指代补全（M12）+ Multi-Query 多路改写（Phase 4 A4）

按 §6 规则：services/ 编排层，可调 core/qwen.py
不动 api/ / intent_service / policy_service

三层防浪费（指代补全）：
- L0 规则检测：含指代词才进入下一步（零成本）
- L1 history 检查：无 history 跳过（零成本）
- L2 LLM 改写：单次 chat 调用补全指代（条件触发）

降级：任意环节失败 → 返原 query（不阻塞业务）

设计取舍：
- 只做指代补全 + Multi-Query，不做 HyDE / 同义词词典（YAGNI：后续 Sprint）
- temperature=0 + 短 prompt + 原 query 必带 → 改坏概率低
- 仅在 product_query / policy_query 路径有效（其他路径不读 query 检索）
- intent 分类前调用：避免「它」「这个」被识别成无效 query

Phase 4 A4 新增 rewrite_query_multi：
- 与 rewrite_query 共用 L0 触发条件（coref_only 模式）
- LLM 一次返回 N 个 JSON 改写变体 → 后端 RRF 融合（policy_service.search_multi_policy）
- 失败降级：返 [原 query] 单元素列表
"""
import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.providers.llm import get_llm_provider
from app.services.config_loader import get_config_loader  # Sprint 4 阶段 5
from app.services.metrics import metrics
from app.services.prompt_loader import get_prompt_loader  # Sprint 4 收尾

logger = logging.getLogger(__name__)


# =============================================================
# 业务规则（启动期加载一次，来自 config/business_rules/query_rewriter.yaml）
# 改阈值/规则 → 改 YAML → 重启服务（roadmap §3.5 不参与热更新）
# 单一真相源：query_rewriter.py 是唯一消费者
# 注：REWRITE_SYSTEM_PROMPT / REWRITE_USER_TEMPLATE 已抽到 config/prompts/query_rewriter/{system,user_template}.yaml
# =============================================================
_RULES = get_config_loader().load("query_rewriter")

# L0：指代词清单（覆盖电商场景常见代词）
# 来源：电商客服多轮对话高频观察 + 中文指代词表精简
# YAML 里 list[str] → re.escape + "|" 拼接 + re.compile（防代词含正则特殊字符时漏匹配）
COREFERENCE_PATTERNS = re.compile(
    "|".join(re.escape(p) for p in _RULES["COREFERENCE_PATTERNS"])
)

# LLM 改写 prompt（启动期从 prompt_loader 加载；Sprint 4 收尾迁移）
# 改 Prompt → 改 YAML → 下次 load() 自动生效（prompt_loader mtime 热更新）
REWRITE_SYSTEM_PROMPT = get_prompt_loader().load("query_rewriter/system")
REWRITE_USER_TEMPLATE = get_prompt_loader().load("query_rewriter/user_template")

# 截短 history：避免 prompt 过长（只取最近 MAX_HISTORY_TURNS 条）
MAX_HISTORY_TURNS = _RULES["MAX_HISTORY_TURNS"]
# history 单条最长字符数
MAX_HISTORY_MSG_LEN = _RULES["MAX_HISTORY_MSG_LEN"]
# 改写结果长度上限：原 query * MAX_REWRITE_RATIO + MAX_REWRITE_EXTRA（防 LLM 输出失控）
MAX_REWRITE_RATIO = _RULES["MAX_REWRITE_RATIO"]
MAX_REWRITE_EXTRA = _RULES["MAX_REWRITE_EXTRA"]

# === Phase 4 A4: Multi-Query 配置（启动期加载一次） ===
ENABLE_MULTI_QUERY = _RULES.get("ENABLE_MULTI_QUERY", settings.ENABLE_MULTI_QUERY)
MULTI_QUERY_COUNT = _RULES.get("MULTI_QUERY_COUNT", settings.MULTI_QUERY_COUNT)
MULTI_QUERY_TRIGGER = _RULES.get("MULTI_QUERY_TRIGGER", settings.MULTI_QUERY_TRIGGER)

# Multi-Query System / User Prompt（启动期从 prompt_loader 加载）
MULTI_SYSTEM_PROMPT_TEMPLATE = get_prompt_loader().load("query_rewriter/multi_system")
MULTI_USER_TEMPLATE = get_prompt_loader().load("query_rewriter/multi_user_template")


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
        result = get_llm_provider().chat(
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


# =============================================================
# Phase 4 A4: Multi-Query 多路改写
# =============================================================
def rewrite_query_multi(
    query: str,
    history: Optional[List[Dict]] = None,
    n: Optional[int] = None,
) -> Tuple[List[str], bool]:
    """
    多路 query 改写：LLM 一次返回 N 个 JSON 改写变体

    与 rewrite_query 的关系：
    - 共用 L0（含 coreference）+ L1（含 history）触发条件
    - 区别：返回 list[str] 而非 str，供 policy_service.search_multi_policy 多路 RRF 融合

    Args:
        query: 用户当前问题
        history: 多轮对话历史 [{"role", "content"}]
        n: 期望变体数；None 时走 settings.MULTI_QUERY_COUNT

    Returns:
        (queries, was_rewritten):
        - queries[0] = 主改写或原 query；长度 = n（不足用原 query 填充）
        - was_rewritten: 是否真触发 LLM（False = 走 shortcut，未产生多路）

    降级策略（任意环节失败 → 单元素列表 [query]）：
    - L0 无 coref → skipped_no_coref
    - L1 无 history → skipped_no_history
    - LLM 异常 → llm_error
    - JSON 解析失败 → parse_fail
    - 变体含数字（可能改订单号/SKU）→ too_long（按 ratio 截断后丢弃该条）
    - 解析出 < 2 条变体 → too_few_variants（降级到 [query] 单路）
    """
    if not query or not query.strip():
        return [query], False
    query = query.strip()

    if n is None:
        n = max(1, MULTI_QUERY_COUNT)

    # L0：规则检测（coref_only 触发条件）
    if MULTI_QUERY_TRIGGER == "coref_only" and not _has_coreference(query):
        metrics.inc_rewrite_multi("skipped_no_coref")
        logger.debug(f"rewrite_multi skip (no coreference): '{query[:30]}...'")
        return [query], False

    # L1：history 检查
    if not history:
        metrics.inc_rewrite_multi("skipped_no_history")
        logger.debug(f"rewrite_multi skip (no history): '{query[:30]}...'")
        return [query], False

    # L2：LLM 多路改写
    history_str = _format_history_snippet(history)

    # system prompt 含 {n} 占位符（运行时注入期望变体数）
    system_prompt = MULTI_SYSTEM_PROMPT_TEMPLATE.replace("{n}", str(n))
    user_prompt = MULTI_USER_TEMPLATE.format(
        history=history_str, query=query, n=n
    )

    try:
        result = get_llm_provider().chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=400,  # N 条变体约 200-300 字足够
        )
        reply = (result.get("reply") or "").strip()

        # 尝试解析：先去掉可能的 markdown 包裹
        if reply.startswith("```"):
            reply = re.sub(r"^```(?:json)?\s*", "", reply)
            reply = re.sub(r"\s*```$", "", reply)

        try:
            parsed = json.loads(reply)
        except json.JSONDecodeError as e:
            logger.warning(
                f"rewrite_multi JSON 解析失败: {e}; raw={reply[:100]}..."
            )
            metrics.inc_rewrite_multi("parse_fail")
            return [query], False

        if not isinstance(parsed, list):
            logger.warning(
                f"rewrite_multi 返回非 list 类型: {type(parsed)}"
            )
            metrics.inc_rewrite_multi("parse_fail")
            return [query], False

        # 校验 + 规范化每条变体
        max_len = len(query) * MAX_REWRITE_RATIO + MAX_REWRITE_EXTRA
        variants: List[str] = []
        for item in parsed:
            if not isinstance(item, str):
                continue
            v = item.strip().strip('"\'""''。. ').strip()
            if not v or len(v) < 2 or len(v) > max_len:
                continue
            variants.append(v)

        # 少于 2 条变体（含 [原 query]）→ 退化为单路
        if len(variants) < 2:
            logger.warning(
                f"rewrite_multi 变体数不足 ({len(variants)}<2)，降级单路: "
                f"query='{query[:30]}...'"
            )
            metrics.inc_rewrite_multi("too_few_variants")
            return [query], False

        # 去重（保留顺序） + padding 用原 query 填充到 n 条
        seen = set()
        deduped: List[str] = []
        for v in variants:
            if v not in seen and v != query:
                seen.add(v)
                deduped.append(v)
        queries = deduped[:n]
        if len(queries) < n:
            # 用原 query 填充（确保 policy_service 收到 n 条，不会"漏"一路）
            queries.extend([query] * (n - len(queries)))

        metrics.inc_rewrite_multi("rewritten")
        logger.info(
            f"rewrite_multi done: {len(queries)} variants "
            f"orig='{query[:30]}...' first='{queries[0][:30]}...'"
        )
        return queries, True
    except Exception as e:
        logger.warning(
            f"rewrite_multi LLM 异常，fallback: query='{query[:30]}...' "
            f"err={type(e).__name__}: {str(e)[:100]}"
        )
        metrics.inc_rewrite_multi("llm_error")
        return [query], False