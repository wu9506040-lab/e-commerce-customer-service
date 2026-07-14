"""
QwenRerankProvider — LLM-based Cross-Encoder Rerank 实现

两阶段检索的"第二阶段"：
1. Qdrant dense vector 检索 top-K 候选（粗排，K=20）
2. 单次 LLM 调用给所有候选打分（精排，取 top-N）

为什么用 LLM 做 rerank（不用专门的 cross-encoder 模型）：
1. 已有 Qwen 客户端，零额外依赖
2. 跨语言 / 多领域适应性好（不需要为每种语言 fine-tune）
3. **单次调用打分全部候选**：比"每候选一次调用"快 20 倍
4. 面试亮点：能讲"为什么不用 BERT cross-encoder / 为什么不用 bge-reranker / 为什么 batch 在一个 prompt"

设计取舍：
- batch 评估：单次 LLM 调用给所有候选打分（prompt 列出 N 个候选，要求返回 JSON 数组）
- score 范围 0-10，prompt 强制整数（方便排序）
- 仅取 payload.text 的前 300 字传给 LLM（控制 token 成本）
- 限制 MAX_CANDIDATES_PER_CALL=15（避免单 prompt 超过 token 上限）
- 解析失败时降级为 0 分（不会崩溃）

CLAUDE.md §6 边界：service 层只做业务编排（调 core/qwen.py + clients/qdrant.py），
不直接调 embedding / 不直接调 HTTP API。

注：本模块的 Prompt / 解析 / batch 截断逻辑由自身实现（不再委托给任何外部模块）。
历史：`app/services/rerank.py` 薄壳已在 Sprint 4 收尾时删除，业务逻辑完全保留于此。
"""
import asyncio
import json
import logging
import re
from typing import List, Dict, Any, Optional

from app.core import qwen as _legacy_qwen
from app.core.providers.rerank.protocols import RerankProvider

logger = logging.getLogger(__name__)

# 限制单 prompt 候选数（避免超过 LLM token 上限）
MAX_CANDIDATES_PER_CALL = 15


def _extract_text_snippet(payload: Dict[str, Any], max_chars: int = 300) -> str:
    """从 Qdrant payload 截取用于 rerank 的文本片段"""
    text = payload.get("text", "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _build_batch_prompt(query: str, candidates: List[Dict[str, Any]]) -> str:
    """
    构造 batch 评估 prompt：要求 LLM 一次给所有候选打分

    输出格式约束为 JSON 数组：[{"id": "idx", "score": int}, ...]
    """
    items = []
    for i, c in enumerate(candidates):
        snippet = _extract_text_snippet(c["payload"])
        items.append(f"【文档{i}】\n{snippet}\n")

    items_str = "\n".join(items)
    n = len(candidates)

    prompt = f"""你是电商客服系统的检索相关性评估员。给定用户问题与 {n} 个候选文档，给每个文档打 0-10 的相关度分。

评分标准：
- 10：完全匹配，直接回答了问题
- 7-9：高度相关，包含问题核心信息
- 4-6：部分相关，提到一些相关内容但不直接
- 1-3：弱相关，仅有少量关键词重合
- 0：完全无关

只输出一个 JSON 数组，按文档顺序给出分数，不要任何额外文字。

用户问题：{query}

{items_str}
输出（JSON 数组，按文档顺序 [{', '.join(f'{{"id":{i}, "score":0}}' for i in range(n))}]）："""
    return prompt


def _parse_batch_scores(reply: str, n: int) -> List[int]:
    """
    解析 LLM 返回的 batch 分数（容错）

    支持 3 种格式（按优先级）：
    1. `[{"id": 0, "score": 8}, ...]` （完整 JSON 对象）
    2. `[7, 4, 1, 1, 1]` （简化：按顺序的分数数组）—— 实际最常见
    3. `0: 7, 1: 4, ...` （key:value 形式）

    失败时返回全 0
    """
    reply = reply.strip()

    # 去掉可能的 markdown 包裹
    if reply.startswith("```"):
        reply = re.sub(r"^```(?:json)?\s*", "", reply)
        reply = re.sub(r"\s*```$", "", reply)

    # 尝试 1：完整 JSON 数组（对象格式）
    try:
        parsed = json.loads(reply)
        if isinstance(parsed, list):
            # 情况 A：list of dicts
            if all(isinstance(x, dict) for x in parsed):
                score_map = {}
                for item in parsed:
                    idx = item.get("id", item.get("idx", item.get("index")))
                    score = item.get("score", 0)
                    if idx is not None:
                        score_map[int(idx)] = max(0, min(10, int(score)))
                return [score_map.get(i, 0) for i in range(n)]
            # 情况 B：list of numbers（按顺序）
            if all(isinstance(x, (int, float)) for x in parsed):
                return [max(0, min(10, int(x))) for x in parsed[:n]]
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 尝试 2：正则提取所有数字（兜底）
    numbers = re.findall(r"\d+", reply)
    if len(numbers) >= n:
        try:
            return [max(0, min(10, int(x))) for x in numbers[:n]]
        except ValueError:
            pass

    logger.warning(f"batch 分数解析失败，reply={reply[:200]}")
    return [0] * n


class QwenRerankProvider:
    """Qwen LLM-based Rerank Provider 实现。

    内部委托给 `app.core.qwen.chat`（含重试 + 断路器）。
    业务逻辑（prompt 构造 + 分数解析 + batch 截断）从原 `services/rerank.py` 迁入，未改。
    """

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_n: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """同步 rerank。

        Args:
            query: 用户问题
            candidates: Qdrant 检索结果 [{"id", "score", "payload"}, ...]
            top_n: 保留前 N 条（None = 全部）

        Returns:
            排序后的 candidates（每条加 "rerank_score" 字段）
        """
        if not candidates:
            return []

        # 截断到单次调用上限
        batch = candidates[:MAX_CANDIDATES_PER_CALL]
        logger.info(f"rerank 开始: query='{query[:30]}...' batch={len(batch)}/{len(candidates)}")

        prompt = _build_batch_prompt(query, batch)
        try:
            result = _legacy_qwen.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0,  # 关闭随机性
                max_tokens=500,
            )
            scores = _parse_batch_scores(result["reply"], len(batch))
        except Exception as e:
            logger.warning(f"rerank 调用失败，降级到原始排序: {e}")
            scores = [0] * len(batch)

        # 合并分数
        for c, s in zip(batch, scores):
            c["rerank_score"] = s

        # 多于 MAX_CANDIDATES_PER_CALL 时，剩余保持原始顺序
        if len(candidates) > MAX_CANDIDATES_PER_CALL:
            for c in candidates[MAX_CANDIDATES_PER_CALL:]:
                c["rerank_score"] = 0

        # 按 rerank_score 降序排
        sorted_candidates = sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)

        logger.debug(
            f"rerank 完成: top5={[(c['id'][:8], c['rerank_score']) for c in sorted_candidates[:5]]}"
        )

        if top_n is not None:
            return sorted_candidates[:top_n]
        return sorted_candidates

    async def rerank_async(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_n: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """异步 rerank（包装同步函数到 default executor）。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.rerank, query, candidates, top_n)