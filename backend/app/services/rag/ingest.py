"""
RAG Ingest - 知识库入库流水线（Qdrant + MySQL metadata 双写 §11）

按 §6 规则：services/ 编排层，可调 core/embedding + clients/qdrant + clients/mysql

流程（write-through §11）：
    原文 → chunk_text() → embed_texts() → qdrant.upsert() → upsert_knowledge_meta()

幂等：
- P1-1：chunk_id = uuid5(source + ":" + chunk_hash[:32])（基于内容 sha256，下标无关）
- MySQL knowledge_documents.source 唯一约束 + UPSERT 保证元数据幂等
- P1-3：MySQL 失败时回滚 Qdrant（防孤儿点）
"""
import hashlib
import logging
import uuid
from typing import Dict, List, Optional

from qdrant_client.models import PointStruct
from sqlalchemy import select

from app.clients.mysql_client import with_safe_session
from app.clients.qdrant import delete_points, ensure_collection, upsert_points
from app.core.config import settings
from app.core.providers.embedding import get_embedding_provider
from app.models.knowledge_document import KnowledgeDocument

logger = logging.getLogger(__name__)

# =============================================================
# 默认配置
# =============================================================
DEFAULT_CHUNK_SIZE = 500   # 每片字符数（中文约 1 char ≈ 1.5 token）
DEFAULT_OVERLAP = 50       # 相邻片重叠字符数（保留上下文连续性）
MIN_CHUNK_SIZE = 100       # 入参下限（防止切得太碎）
MAX_CHUNK_SIZE = 2000      # 入参上限（防止单片过大影响 embedding 质量）
MAX_TEXT_LENGTH = 100_000  # 单次请求原文上限（~100KB，保护服务）


# =============================================================
# 切片
# =============================================================
# =============================================================
# chunk_id 生成（P1-1：基于内容 hash 稳定化）
# =============================================================
def compute_chunk_id(
    source: str,
    text: str,
    position: Optional[int] = None,
) -> str:
    """
    计算 chunk_id（P1-1）

    新逻辑（开关启用时）：uuid5(source + ":" + chunk_hash[:32])
        - 基于内容 sha256 → 同一文本永远同 ID → 重跑幂等、增量更新安全
    旧逻辑（开关关闭时）：uuid5(source + ":" + position)
        - 基于下标 → 仅 A/B 对比用，不推荐生产

    Args:
        source: 来源标识（如文件名）
        text: chunk 文本内容
        position: chunk 在原文中的下标（仅旧逻辑使用；新逻辑忽略）

    Returns:
        UUID 字符串（36 字符）
    """
    if settings.RAG_CHUNK_ID_BY_CONTENT_HASH:
        chunk_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        seed = f"{source}:{chunk_hash}"
    else:
        # 旧逻辑：必须有 position（与 M14 V3 前兼容）
        if position is None:
            raise ValueError("compute_chunk_id: 旧逻辑（开关关闭）必须传 position")
        seed = f"{source}:{position}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[str]:
    """
    字符级滑动窗口切片

    Args:
        text: 原文
        chunk_size: 每片字符数（100-2000）
        overlap: 重叠字符数（0 到 chunk_size-1）

    Returns:
        切片列表（已 strip，不含空片）
    """
    # 参数校验
    if not isinstance(text, str):
        raise ValueError("chunk_text: text 必须是 str")
    if chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
        raise ValueError(
            f"chunk_size 必须在 [{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}]，收到 {chunk_size}"
        )
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError(
            f"overlap 必须在 [0, {chunk_size})，收到 {overlap}"
        )

    text = text.strip()
    if not text:
        return []

    chunks: List[str] = []
    n = len(text)
    start = 0
    step = chunk_size - overlap

    while start < n:
        end = min(start + chunk_size, n)
        piece = text[start:end].strip()
        if piece:  # 过滤纯空白片
            chunks.append(piece)
        if end >= n:
            break
        start += step

    logger.info(
        f"chunk_text: text_len={n}, chunk_size={chunk_size}, "
        f"overlap={overlap}, chunks={len(chunks)}"
    )
    return chunks


# =============================================================
# MySQL 元数据 upsert（§11 write-through）
# =============================================================
def upsert_knowledge_meta(
    source: str,
    total_chunks: int,
    total_chars: int,
    uploader_id: Optional[int] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    doc_type: str = "manual",
) -> Optional[KnowledgeDocument]:
    """
    UPSERT knowledge_documents 元数据行

    失败仅 warning，不抛（MySQL 是冷路径，挂掉不影响 Qdrant 写入）

    Returns:
        KnowledgeDocument 或 None（失败时）
    """
    # with_safe_session 内部 commit + 异常吞咽 + warning
    # 注意：db.refresh(doc) 必须在 with 块内（close 前）执行
    doc: Optional[KnowledgeDocument] = None
    success = False
    with with_safe_session(commit=True) as db:
        existing = db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source == source,
                KnowledgeDocument.deleted == 0,
            )
        ).scalar_one_or_none()

        if existing:
            # 覆盖：更新 chunks/chars/status，title/desc/uploader 取新值或保留旧值
            existing.total_chunks = total_chunks
            existing.total_chars = total_chars
            existing.status = 1  # 重新入库 = 上线
            if title is not None:
                existing.title = title
            if description is not None:
                existing.description = description
            if uploader_id is not None:
                existing.uploader_id = uploader_id
            doc = existing
        else:
            doc = KnowledgeDocument(
                source=source,
                title=title,
                description=description,
                doc_type=doc_type,
                total_chunks=total_chunks,
                total_chars=total_chars,
                uploader_id=uploader_id,
                status=1,
            )
            db.add(doc)

        # refresh 必须在 close 前（expire_on_commit=False 已设，commit 后字段可访问，
        # 但 refresh 强制重读是拿自增 id 的标准做法）
        db.refresh(doc)
        logger.info(
            f"upsert_knowledge_meta: source={source}, "
            f"chunks={total_chars}, uploader_id={uploader_id}, "
            f"id={doc.id}"
        )
        success = True  # 只有 refresh + log 都成功才认为成功

    return doc if success else None


# =============================================================
# 入库（Qdrant + MySQL 双写）
# =============================================================
def ingest_text(
    text: str,
    source: str = "manual",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    uploader_id: Optional[int] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    # 修复：原版漏掉 doc_type，导致所有元数据都默认 'manual'，分类能力失效
    # 电商知识库等场景需要按 doc_type 区分（product / policy / faq）
    doc_type: str = "manual",
) -> Dict:
    """
    把一段原文切分后入库到 Qdrant + MySQL 元数据

    Args:
        text: 原文（≤ 100KB）
        source: 来源标识（如文件名，幂等性 key）
        chunk_size: 切片大小
        overlap: 重叠大小
        uploader_id: 上传者用户 ID（§11 write-through）
        title: 文档标题（§11）
        description: 文档描述（§11）

    Returns:
        {
            "ingested_chunks": int,
            "source": str,
            "chunk_ids": List[str],
            "chunk_size": int,
            "overlap": int,
        }
    """
    if not text or not text.strip():
        raise ValueError("ingest_text: text 不能为空")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(
            f"ingest_text: text 长度 {len(text)} 超过上限 {MAX_TEXT_LENGTH}"
        )

    # 1. 切片
    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return {
            "ingested_chunks": 0,
            "source": source,
            "chunk_ids": [],
            "chunk_size": chunk_size,
            "overlap": overlap,
        }

    # 2. 确保 collection 存在（首次入库场景）
    ensure_collection()

    # 3. 批量 embedding
    vectors = get_embedding_provider().embed_texts(chunks)

    # 4. 构造 PointStruct
    # P1-1：chunk_id 基于内容 hash 而非下标（compute_chunk_id 封装开关逻辑）
    # P3-3：doc_type 写入 Qdrant payload，RRF 类型加权的数据来源
    chunk_ids: List[str] = []
    points: List[PointStruct] = []
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        point_id = compute_chunk_id(source, chunk, position=i)
        chunk_ids.append(point_id)
        points.append(
            PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "text": chunk,
                    "source": source,
                    "chunk_index": i,
                    "doc_type": doc_type,  # P3-3：给 RRF 加权用
                },
            )
        )

    # 5. 写入 Qdrant（真源）
    qdrant_written = upsert_points(points)

    # 5.5 P1-2：触发 BM25 索引后台异步重建（不阻塞主流程）
    # 收益：避免下次 bm25_search 调用时懒加载 1-3s RT spike
    # 关闭开关时保留懒加载（与 P1-2 之前行为一致）
    if qdrant_written > 0 and settings.RAG_BM25_EAGER_BUILD:
        try:
            from app.services.bm25_index import invalidate_and_rebuild_async
            invalidate_and_rebuild_async()
            logger.debug(f"ingest_text: BM25 索引后台重建已触发（source={source}）")
        except Exception:
            # 后台重建启动失败不影响主流程（懒加载兜底）
            logger.exception(f"ingest_text: BM25 异步重建触发失败（source={source}），下次 search 将懒加载")

    # 6. write-through：同步 MySQL 元数据（§11）
    # P1-3 修复：MySQL 失败时回滚 Qdrant，防止孤儿点残留
    # - upsert_knowledge_meta 内部用 with_safe_session 吞咽异常并返 None
    # - doc is None 即为 MySQL 失败信号（语义保留：不抛异常，与原行为兼容）
    total_chars = sum(len(c) for c in chunks)
    doc = upsert_knowledge_meta(
        source=source,
        total_chunks=len(chunks),
        total_chars=total_chars,
        uploader_id=uploader_id,
        title=title,
        description=description,
        doc_type=doc_type,  # 修复：原版漏传，导致元数据全部默认 'manual'
    )

    # P1-3 rollback：Qdrant 真写了点 + MySQL metadata 失败 + 开关启用 → 删 Qdrant 点
    # 注意：qdrant_written=0（断路器开路场景）时无须回滚（Qdrant 实际没写）
    if (
        doc is None
        and qdrant_written > 0
        and settings.RAG_ROLLBACK_ON_MYSQL_FAIL
    ):
        try:
            delete_points(chunk_ids)
            logger.warning(
                f"ingest_text rollback: MySQL metadata 写入失败，"
                f"已删除 Qdrant {len(chunk_ids)} 个点（source={source}）"
            )
        except Exception:
            # 回滚本身失败：log warning + 异常栈，但不重新抛
            # 原因：不掩盖原 MySQL 失败信号（doc is None 已透出给调用方）
            # 兜底清理：可后续 P1-1 / P1-2 加定时 sweep_orphan_qdrant.py 脚本
            logger.exception(
                f"ingest_text rollback FAILED: MySQL 失败 + Qdrant delete 也失败，"
                f"需人工清理 orphan_points={chunk_ids[:3]}... (共 {len(chunk_ids)} 个)"
            )

    logger.info(
        f"ingest_text: source={source}, chunks={len(chunks)}, "
        f"total_chars={total_chars}, uploader_id={uploader_id}"
    )

    return {
        "ingested_chunks": len(chunks),
        "source": source,
        "chunk_ids": chunk_ids,
        "chunk_size": chunk_size,
        "overlap": overlap,
    }
