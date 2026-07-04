"""
BM25 倒排索引（lexical 检索）

为什么不用 jieba / rank_bm25 第三方：
- 项目禁止乱装依赖（CLAUDE.md）
- 中文 char 2-gram 切词在短文档（<500字）召回质量与 jieba 接近
- 内联实现 BM25 仅 ~30 行，无外部依赖

设计：
- Corpus 从 Qdrant 懒加载（首次调用 search 时构建），缓存到内存
- 切词：char 2-gram（覆盖中文 + 英文数字），同时保留单字避免过细切
- Okapi BM25 标准公式（k1=1.5, b=0.75）
- invalidate() 用于 ingest 后强制重建索引

为什么不替换 Qdrant 向量检索：
- BM25 是稀疏向量检索，与 dense vector 互补
- 混合检索（dense + BM25）能解决"关键词精确命中但语义不相似"的 case
  例：用户搜"ZP2 Pro Max 续航"，vector 召回含"续航"语义的 doc，
     BM25 召回精确含"ZP2 Pro Max"的 doc，RRF 融合后两者都进 top
"""
import logging
import re
import threading
from typing import List, Dict, Any, Optional, Tuple

from app.clients.qdrant import search as qdrant_search, QDRANT_COLLECTION
from app.core.embedding import embed_text
from app.core.config import settings

logger = logging.getLogger(__name__)


# ==================== Tokenization ====================

def _tokenize(text: str) -> List[str]:
    """中文 char 2-gram + 英文单词切词

    例：
        "7天无理由退货运费险" → ['7天', '天无', '无理由', '理由', '由退', '退货', '货运', '运险']
        "ZP2 Pro Max 续航" → ['zp2', 'pro', 'max', '续航']
    """
    text = text.lower().strip()
    if not text:
        return []

    # 1. 提取连续的英文/数字 token
    word_pattern = re.compile(r"[a-z0-9]+")
    words = word_pattern.findall(text)

    # 2. 中文按字符 2-gram 切
    chinese_chars = re.sub(r"[a-z0-9\s]+", "", text)
    bigrams = []
    for i in range(len(chinese_chars)):
        # 单字
        bigrams.append(chinese_chars[i])
        # 2-gram
        if i + 1 < len(chinese_chars):
            bigrams.append(chinese_chars[i:i + 2])

    return words + bigrams


# ==================== BM25 评分 ====================

class BM25Okapi:
    """Okapi BM25 实现（标准 k1=1.5, b=0.75）"""

    def __init__(self, corpus: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = corpus
        self.N = len(corpus)
        self.doc_len = [len(doc) for doc in corpus]
        self.avgdl = sum(self.doc_len) / self.N if self.N > 0 else 0.0
        # df[t] = 包含 term t 的文档数
        self.df: Dict[str, int] = {}
        for doc in corpus:
            for term in set(doc):
                self.df[term] = self.df.get(term, 0) + 1
        # 预计算 IDF
        import math
        self.idf: Dict[str, float] = {}
        for term, df in self.df.items():
            # 标准 IDF 公式（带 +1 防负数）
            self.idf[term] = math.log((self.N - df + 0.5) / (df + 0.5) + 1.0)

    def score(self, query_tokens: List[str]) -> List[float]:
        """对每篇文档计算 BM25 分"""
        scores = [0.0] * self.N
        for term in query_tokens:
            if term not in self.df:
                continue
            idf = self.idf[term]
            for i, doc in enumerate(self.corpus):
                tf = doc.count(term)
                if tf == 0:
                    continue
                dl = self.doc_len[i]
                # 标准 BM25 公式
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl) if self.avgdl > 0 else tf + self.k1
                scores[i] += idf * (numerator / denominator)
        return scores


# ==================== 懒加载索引 ====================

_INDEX_LOCK = threading.Lock()
_INDEX: Optional[Dict[str, Any]] = None  # {"docs": [...], "bm25": BM25Okapi, "doc_id_map": {qdrant_id: idx}}


def _build_index() -> Dict[str, Any]:
    """从 Qdrant 全量拉取 docs，构建 BM25 索引

    注意：scroll 整个 collection 会拉所有点，67 条 policy 约 <100KB，
    内存压力可忽略。如未来 KB 增长到 1 万+，考虑分批 + 增量。
    """
    from qdrant_client import QdrantClient
    from app.clients.qdrant import get_client

    client = get_client()
    logger.info(f"BM25 索引构建开始：从 '{QDRANT_COLLECTION}' 全量拉取")

    # 用 scroll 拉全部点（不分页是因为 collection < 100 条）
    records, _ = client.scroll(
        collection_name=QDRANT_COLLECTION,
        limit=10000,
        with_payload=True,
        with_vectors=False,
    )

    docs = []
    tokenized_corpus = []
    doc_id_map = {}  # qdrant point id → corpus 索引

    for idx, rec in enumerate(records):
        text = (rec.payload or {}).get("text", "")
        if not text:
            continue
        tokenized = _tokenize(text)
        if not tokenized:
            continue
        docs.append({
            "id": str(rec.id),
            "text": text,
            "source": (rec.payload or {}).get("source", ""),
            "payload": rec.payload or {},
        })
        tokenized_corpus.append(tokenized)
        doc_id_map[str(rec.id)] = idx

    bm25 = BM25Okapi(tokenized_corpus)

    logger.info(
        f"BM25 索引构建完成: docs={len(docs)}, vocab={len(bm25.df)}, "
        f"avgdl={bm25.avgdl:.1f}"
    )
    return {"docs": docs, "bm25": bm25, "doc_id_map": doc_id_map}


def _get_index() -> Dict[str, Any]:
    """获取索引（懒加载 + 线程安全）"""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is not None:  # double-check
            return _INDEX
        _INDEX = _build_index()
        return _INDEX


def invalidate() -> None:
    """手动失效缓存（ingest 后调用）"""
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = None
    logger.info("BM25 索引已失效，下次 search 将重建")


def bm25_search(query: str, top_k: int = 15) -> List[Dict[str, Any]]:
    """
    BM25 检索（纯词法，无向量）

    Args:
        query: 用户问题
        top_k: 返回前 N 条

    Returns:
        [{"id", "text", "source", "score", "payload"}, ...]
        按 BM25 分降序排；空索引时返回 []
    """
    try:
        index = _get_index()
    except Exception as e:
        logger.warning(f"BM25 索引构建失败，降级到空结果: {e}")
        return []

    if not index["docs"]:
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    scores = index["bm25"].score(query_tokens)

    # 取 top-k（按 BM25 分降序排）
    scored_docs = [
        {**index["docs"][i], "score": float(scores[i])}
        for i in range(len(scores)) if scores[i] > 0
    ]
    scored_docs.sort(key=lambda x: x["score"], reverse=True)

    return scored_docs[:top_k]


def index_stats() -> Dict[str, Any]:
    """用于 /health 或调试：返回索引统计"""
    try:
        idx = _get_index()
        return {
            "loaded": True,
            "doc_count": len(idx["docs"]),
            "vocab_size": len(idx["bm25"].df),
            "avgdl": round(idx["bm25"].avgdl, 1),
        }
    except Exception as e:
        return {"loaded": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}