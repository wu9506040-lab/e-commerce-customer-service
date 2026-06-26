"""
RAG Ingest - 知识库入库流水线（Qdrant + MySQL metadata 双写 §11）

按 §6 规则：services/ 编排层，可调 core/embedding + clients/qdrant + clients/mysql

流程（write-through §11）：
    原文 → chunk_text() → embed_texts() → qdrant.upsert() → upsert_knowledge_meta()

幂等：uuid5(source:index) 保证 Qdrant 同 source 重跑幂等；
     MySQL knowledge_documents.source 唯一约束 + UPSERT 保证元数据幂等。
"""
import logging
import uuid
from typing import Dict, List, Optional

from qdrant_client.models import PointStruct
from sqlalchemy import select

from app.clients.mysql_client import with_safe_session
from app.clients.qdrant import ensure_collection, upsert_points
from app.core.embedding import embed_texts
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
    vectors = embed_texts(chunks)

    # 4. 构造 PointStruct（用 uuid5 保证同 source 重跑幂等）
    chunk_ids: List[str] = []
    points: List[PointStruct] = []
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{source}:{i}"))
        chunk_ids.append(point_id)
        points.append(
            PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "text": chunk,
                    "source": source,
                    "chunk_index": i,
                },
            )
        )

    # 5. 写入 Qdrant（真源）
    upsert_points(points)

    # 6. write-through：同步 MySQL 元数据（§11）
    total_chars = sum(len(c) for c in chunks)
    upsert_knowledge_meta(
        source=source,
        total_chunks=len(chunks),
        total_chars=total_chars,
        uploader_id=uploader_id,
        title=title,
        description=description,
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
