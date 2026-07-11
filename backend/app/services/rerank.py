"""
rerank.py — DEPRECATED shim（Sprint 1, 2026-07-11）

⚠️ DEPRECATED：本模块保留仅为兼容旧 import 路径，业务模块应改用
`app.core.providers.rerank.get_rerank_provider()`。删除计划：S4 末。

历史：原业务逻辑（prompt 构造 + 分数解析 + batch 截断）已迁入
`app.core.providers.rerank.qwen_provider.QwenRerankProvider`。
本 shim 仅保持 `rerank()` / `rerank_async()` 函数签名，业务模块可继续 import。

为什么用 LLM 做 rerank（面试亮点）：
1. 已有 Qwen 客户端，零额外依赖
2. 跨语言 / 多领域适应性好（不需要为每种语言 fine-tune）
3. **单次调用打分全部候选**：比"每候选一次调用"快 20 倍
"""
from typing import List, Dict, Any, Optional

from app.core.providers.rerank import get_rerank_provider


def rerank(
    query: str,
    candidates: List[Dict[str, Any]],
    top_n: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """同步版 rerank（脚本调用入口）。

    DEPRECATED: use `get_rerank_provider().rerank()`。
    行为完全等价：委托给 `QwenRerankProvider.rerank`。
    """
    return get_rerank_provider().rerank(query, candidates, top_n)


async def rerank_async(
    query: str,
    candidates: List[Dict[str, Any]],
    top_n: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """异步版 rerank。

    DEPRECATED: use `get_rerank_provider().rerank_async()`。
    """
    return await get_rerank_provider().rerank_async(query, candidates, top_n)