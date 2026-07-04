"""
RRF (Reciprocal Rank Fusion) - 多路检索结果融合

为什么用 RRF 不用 linear combination：
1. 不同检索器的分数分布差异巨大（vector cosine 0-1，BM25 可达 10+）
   linear combo 需要归一化，调权麻烦
2. RRF 只用排名信息，对分数绝对值不敏感，天然鲁棒
3. Cormack et al. 2009 证明 RRF 在多路融合上 SOTA 或 near-SOTA
4. 无需训练，无需调参（仅 k 一个超参，常用 60）

公式：
    fused_score(d) = sum over rankers i:  1 / (k + rank_i(d))
    其中 rank_i(d) = d 在第 i 路结果中的排名（1-based，未命中 = 不贡献）

设计要点：
- 多路输入：list of rank lists（每路 = list of {"id": ..., "score": ..., ...}）
- 输出：按 fused_score 降序排，每个 doc 加 rrf_score 字段
- 文档 ID 优先用 "id" 字段（Qdrant point ID），缺省时回退 "source"（BM25 corpus）
- 未在某些路命中的文档 = 在该路 rank = ∞（不贡献分数）
"""
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

# RRF k 常数（Cormack 论文推荐 60，社区广泛使用）
DEFAULT_K = 60


def rrf_fuse(
    rank_lists: List[List[Dict[str, Any]]],
    k: int = DEFAULT_K,
    id_field: str = "id",
) -> List[Dict[str, Any]]:
    """
    RRF 多路融合

    Args:
        rank_lists: 多路检索结果，每路是按相关度降序排的 list
        k: RRF 常数（默认 60）

    Returns:
        按 rrf_score 降序排的 docs，每个 doc 含 rrf_score + source_ranks 字段
    """
    fused: Dict[str, Dict[str, Any]] = {}  # doc_id -> {"doc": ..., "rrf_score": float, "source_ranks": [int]}

    for list_idx, rank_list in enumerate(rank_lists):
        for rank, doc in enumerate(rank_list, start=1):  # rank 从 1 开始
            doc_id = doc.get(id_field) or doc.get("source")
            if not doc_id:
                logger.warning(f"RRF 跳过无 ID 的 doc: {doc}")
                continue
            doc_id = str(doc_id)

            if doc_id not in fused:
                # 第一路命中：初始化（保留 doc 原信息）
                fused[doc_id] = {
                    "doc": doc,
                    "rrf_score": 0.0,
                    "source_ranks": [],
                }
            # 累加 RRF 分数
            fused[doc_id]["rrf_score"] += 1.0 / (k + rank)
            fused[doc_id]["source_ranks"].append((list_idx, rank))

    # 按 rrf_score 降序排
    sorted_results = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)

    # 整理输出格式：合并 doc 原字段 + 加 rrf_score
    results = []
    for entry in sorted_results:
        result = dict(entry["doc"])
        result["rrf_score"] = round(entry["rrf_score"], 6)
        result["source_ranks"] = entry["source_ranks"]
        results.append(result)

    return results