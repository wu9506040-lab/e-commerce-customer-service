"""
RerankProvider Protocol — 检索重排序抽象

按 CLAUDE.md §9.3.3：业务模块禁止直接调用第三方 SDK 做 rerank。
"""
from typing import Protocol, List, Dict, Any, Optional, runtime_checkable


@runtime_checkable
class RerankProvider(Protocol):
    """Rerank 能力抽象。

    业务模块通过 `get_rerank_provider()` 获取实例。
    当前唯一实现：QwenRerankProvider（基于 LLM batch 评估）。
    """

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_n: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """同步 rerank：对 candidates 按 query 相关性重排，每条加 'rerank_score' 字段。

        Args:
            query: 用户问题
            candidates: 检索结果 [{"id", "score", "payload"}, ...]
            top_n: 保留前 N 条（None = 全部）

        Returns:
            排序后的 candidates（按 rerank_score 降序）
        """
        ...

    async def rerank_async(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_n: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """异步 rerank（包装同步函数到 default executor）。

        异步路径只是 sync 的 thread offload，不重做 batch 逻辑。
        """
        ...