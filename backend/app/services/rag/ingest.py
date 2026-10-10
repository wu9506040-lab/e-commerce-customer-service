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
from app.clients.qdrant import (
    delete_points,
    delete_source_points,
    ensure_collection,
    upsert_points,
)
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


def _validate_chunk_params(
    text: str, chunk_size: int, overlap: int, fname: str = "chunk_text"
) -> None:
    """切片公共参数校验（chunk_text / chunk_text_semantic 共用）。"""
    if not isinstance(text, str):
        raise ValueError(f"{fname}: text 必须是 str")
    if chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
        raise ValueError(
            f"chunk_size 必须在 [{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}]，收到 {chunk_size}"
        )
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError(
            f"overlap 必须在 [0, {chunk_size})，收到 {overlap}"
        )


def _split_sentences(text: str) -> List[str]:
    """按中文/英文终止符（。！？；!?;）与换行切句；终止符归属前句、空白段丢弃。"""
    sents: List[str] = []
    buf: List[str] = []
    for ch in text:
        buf.append(ch)
        if ch in "。！？；!?;\n":
            s = "".join(buf).strip()
            if s:
                sents.append(s)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        sents.append(tail)
    return sents


def chunk_text_semantic(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[str]:
    """V13（2.1'，2026-10-10）：句边界贪心切片。

    规则：
    - 句子为原子单位，贪心装箱至超 chunk_size 截断——chunk 边界必落在句末，
      不再切断从句（旧 500 字符滑窗会把"退货满 30 元、且需凭……"拦腰斩断）
    - overlap 以"整句携带"实现：从上一片尾部倒序携带累计长度 ≤ overlap 的句子
    - 单句超长（>chunk_size）时仅该句退回字符滑窗，保证不丢内容

    与 chunk_text（字符滑窗）行为差异均为有意设计；对照实验走
    settings.RAG_CHUNK_STRATEGY="char" 回退旧策略。
    """
    _validate_chunk_params(text, chunk_size, overlap, fname="chunk_text_semantic")
    sents = _split_sentences(text)
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0

    for s in sents:
        if len(s) > chunk_size:
            # 超长单句：冲掉当前片，该句内部退回字符滑窗
            if cur:
                chunks.append("".join(cur).strip())
                cur, cur_len = [], 0
            step = max(chunk_size - overlap, 1)
            i = 0
            while i < len(s):
                piece = s[i:i + chunk_size].strip()
                if piece:
                    chunks.append(piece)
                if i + chunk_size >= len(s):
                    break
                i += step
            continue
        if cur and cur_len + len(s) > chunk_size:
            chunks.append("".join(cur).strip())
            # overlap：从刚发出片的尾部整句倒序携带（累计不超过 overlap 字符）
            tail: List[str] = []
            tl = 0
            for x in reversed(cur):
                if tl + len(x) > overlap:
                    break
                tail.insert(0, x)
                tl += len(x)
            cur, cur_len = tail, tl
        cur.append(s)
        cur_len += len(s)

    if cur:
        last = "".join(cur).strip()
        if last:
            chunks.append(last)

    logger.info(
        f"chunk_text_semantic: text_len={len(text)}, "
        f"chunk_size={chunk_size}, overlap={overlap}, chunks={len(chunks)}"
    )
    return chunks


# =============================================================
# doc_type 词表归一（V13 2.1'）
# =============================================================
def normalize_doc_type(raw: Optional[str], source: Optional[str] = None) -> str:
    """把自由生长的 doc_type 归一到 RAG_TYPE_BOOST 认识的词表。

    背景（2026-10-10 审计）：P3-3 类型加权配置只认 policy/faq/product 三键，
    而实际 KB 文件里散落 *_policy / faq_* / product_sku* / promotion /
    platform_compare 等 13 种写法——绝大多数文档拿不到加权，boost 名存实亡。
    未识别的值原样保留（保守：不发明类目）。
    """
    key = f"{(raw or '').lower()} {(source or '').lower()}"
    if "faq" in key or "12315" in key:
        return "faq"
    if "promo" in key or "coupon" in key or "活动" in key:
        return "promo"
    if "product" in key or "sku" in key:
        return "product"
    for kw in ("policy", "warranty", "shipping", "return", "refund",
               "dispute", "supplement", "platform", "保修", "物流", "退货", "政策"):
        if kw in key:
            return "policy"
    return (raw or "").strip() or "manual"


def chunk_text_auto(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[str]:
    """按 settings.RAG_CHUNK_STRATEGY 分发（"sentence" 默认 / "char" 回退旧滑窗）。

    ingest 与 admin 上传统一走本入口；chunk_id 仍按切片正文内容哈希——
    策略切换只改变切片结果，不引入 ID 计算口径分叉。
    """
    strategy = getattr(settings, "RAG_CHUNK_STRATEGY", "sentence")
    if strategy == "sentence":
        return chunk_text_semantic(text, chunk_size=chunk_size, overlap=overlap)
    return chunk_text(text, chunk_size=chunk_size, overlap=overlap)


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[str]:
    """
    字符级滑动窗口切片（旧策略，保留作 A/B 对照与回退）

    Args:
        text: 原文
        chunk_size: 每片字符数（100-2000）
        overlap: 重叠字符数（0 到 chunk_size-1）

    Returns:
        切片列表（已 strip，不含空片）
    """
    # 参数校验
    _validate_chunk_params(text, chunk_size, overlap, fname="chunk_text")

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

        # T2.4 致命问题7 修复：db.refresh(doc) → db.flush()
        # 根因：db.refresh() 在 new (未持久化) 对象上是非法操作，抛
        #   "Instance '<KnowledgeDocument>' is not persistent within this Session"
        # 旧版 with_safe_session 在 except 块吞咽这条异常 → success=False → return None
        #   → ingest_text P1-3 rollback 删新 Qdrant 点 → 只剩旧的脏数据
        # 改 flush()：立刻发送 INSERT，让 autogen PK 填充 doc.id（不需要真正 commit）
        # 对于 existing（已 SELECT 加载），flush 不发 SQL，无副作用
        db.flush()
        logger.info(
            f"upsert_knowledge_meta: source={source}, "
            f"chunks={total_chars}, uploader_id={uploader_id}, "
            f"id={doc.id}"
        )
        success = True  # 只有 flush + log 都成功才认为成功

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

    # 1. 切片（V13 2.1'：走策略分发入口，默认句边界贪心，可回退字符滑窗）
    chunks = chunk_text_auto(text, chunk_size=chunk_size, overlap=overlap)
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

    # 2.5 V13（2.1'）delete-before-write：先清空同 source 全部旧点再写新点。
    # 根治审计缺陷：内容哈希 ID 下"切片文本一变 → ID 变 → 旧点无人删"→
    # 库内多世代混存（2026-10-10 实测线上 ~45% 陈旧点即此根因）。
    # 清理失败不阻断入库（upsert 本身按内容哈希幂等，仅可能残留旧点，
    # 由 scripts/prune_orphan_sources.py 兜底清扫）。
    try:
        deleted_old = delete_source_points(source)
        if deleted_old:
            logger.info(f"ingest_text: source={source} 预清旧点 {deleted_old} 个")
    except Exception as e:
        logger.warning(f"ingest_text: delete-before-write 失败（继续入库）: {e}")

    # 3. 批量 embedding
    # V13（2.1'）title 增强：embedding 文本带【标题】锚点（弱上下文 chunk 的
    # 语义向量获得主题先验，Anthropic contextual retrieval 的轻量版）；
    # chunk_id 仍按正文内容哈希——标题措辞变动不影响 ID 稳定；
    # payload.text 保持原文纯净（引用展示与"数字逐字引用"约束不被污染）
    embed_inputs = [f"【{title}】{c}" if title else c for c in chunks]
    vectors = get_embedding_provider().embed_texts(embed_inputs)

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
                    "title": title or "",  # V13（2.1'）：标题入 payload（展示/过滤用）
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
