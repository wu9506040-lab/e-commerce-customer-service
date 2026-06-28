"""
Admin HTTP 接口层（全部需 admin 角色 + write-through §11 + audit）

按 §6 规则：
- api/ 只负责路由 + 参数解析 + 调 services
- 不写业务逻辑

§10 起：所有端点加 require_admin 鉴权
§11 起：ingest 透传 uploader_id/title/description；delete 触发 MySQL 软删

实现：
    POST   /admin/ingest                ingest_text
    GET    /admin/knowledge/info        knowledge.get_info
    GET    /admin/knowledge/sources     knowledge.list_sources
    DELETE /admin/knowledge/source/{s}  knowledge.delete_by_source
    DELETE /admin/knowledge/points      knowledge.delete_by_ids
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from app.api.deps import require_admin
from app.models.user import User
from app.schemas.admin import (
    DeleteByIdsRequest,
    DeleteResponse,
    IngestRequest,
    IngestResponse,
    KnowledgeInfoResponse,
    SourceListItem,
    SourceListResponse,
)
from app.services.audit_service import try_log_action
from app.services.rag.ingest import ingest_text
from app.services.rag.knowledge import (
    delete_by_ids as k_delete_by_ids,
    delete_by_source as k_delete_by_source,
    get_info as k_get_info,
    list_sources as k_list_sources,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 入库慢（embed + upsert），给 60s
INGEST_TIMEOUT = 60.0
# 查询/删除走 qdrant 本地操作，给 10s
MGMT_TIMEOUT = 10.0


def _admin_ctx(admin: User) -> str:
    """日志用的 admin 标识"""
    return f"admin={admin.username}(id={admin.id})"


def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _user_agent(request: Request) -> Optional[str]:
    ua = request.headers.get("user-agent", "")
    return ua[:500] if ua else None


# =============================================================
# 入库（Qdrant + MySQL 双写 §11）
# =============================================================
@router.post(
    "/ingest",
    response_model=IngestResponse,
    summary="知识库入库",
    description="把原文切分、embed 后写入 Qdrant，同步写 MySQL 元数据。同 source 二次入库幂等。需 admin 权限。",
)
async def ingest(
    request: Request,
    payload: IngestRequest,
    admin: User = Depends(require_admin),
):
    ip = _client_ip(request)
    ua = _user_agent(request)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                ingest_text,
                payload.text,
                payload.source,
                payload.chunk_size,
                payload.overlap,
                admin.id,                  # uploader_id（§11 write-through）
                payload.title,             # 文档标题（§11）
                payload.description,       # 文档描述（§11）
            ),
            timeout=INGEST_TIMEOUT,
        )
        logger.info(
            f"/admin/ingest ok: {_admin_ctx(admin)}, "
            f"source={payload.source}, chunks={result['ingested_chunks']}, "
            f"text_len={len(payload.text)}"
        )
        try_log_action(
            user=admin,
            action="ingest",
            target_type="knowledge",
            target_id=payload.source,
            ip=ip,
            user_agent=ua,
            detail={
                "chunks": result["ingested_chunks"],
                "text_len": len(payload.text),
                "chunk_size": payload.chunk_size,
                "overlap": payload.overlap,
            },
        )
        return IngestResponse(**result)
    except asyncio.TimeoutError:
        logger.error(
            f"/admin/ingest timeout: {_admin_ctx(admin)}, "
            f"source={payload.source}, text_len={len(payload.text)}"
        )
        try_log_action(
            user=admin,
            action="ingest",
            target_type="knowledge",
            target_id=payload.source,
            ip=ip,
            user_agent=ua,
            result="fail",
            error_msg=f"timeout after {INGEST_TIMEOUT}s",
        )
        raise HTTPException(status_code=504, detail=f"入库超时（>{INGEST_TIMEOUT}s）")
    except ValueError as e:
        logger.error(f"/admin/ingest 参数错误: {_admin_ctx(admin)}, {e}")
        try_log_action(
            user=admin,
            action="ingest",
            target_type="knowledge",
            target_id=payload.source,
            ip=ip,
            user_agent=ua,
            result="fail",
            error_msg=str(e)[:500],
        )
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"/admin/ingest 调用失败: {_admin_ctx(admin)}")
        try_log_action(
            user=admin,
            action="ingest",
            target_type="knowledge",
            target_id=payload.source,
            ip=ip,
            user_agent=ua,
            result="fail",
            error_msg=f"{type(e).__name__}: {str(e)[:300]}",
        )
        raise HTTPException(status_code=500, detail=f"入库失败: {type(e).__name__}")


# =============================================================
# 知识库管理
# =============================================================
@router.get(
    "/knowledge/info",
    response_model=KnowledgeInfoResponse,
    summary="知识库统计",
    description="返回 collection 的点数、向量维度、状态等。需 admin 权限。",
)
async def knowledge_info(admin: User = Depends(require_admin)):
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(k_get_info),
            timeout=MGMT_TIMEOUT,
        )
        return KnowledgeInfoResponse(**info)
    except Exception as e:
        logger.exception(f"/admin/knowledge/info 失败: {_admin_ctx(admin)}")
        raise HTTPException(status_code=500, detail=f"查询失败: {type(e).__name__}")


@router.get(
    "/knowledge/sources",
    response_model=SourceListResponse,
    summary="来源列表",
    description="按 source 字段聚合，统计每个来源的 chunk 数。需 admin 权限。",
)
async def knowledge_sources(admin: User = Depends(require_admin)):
    try:
        items = await asyncio.wait_for(
            asyncio.to_thread(k_list_sources),
            timeout=MGMT_TIMEOUT,
        )
        source_items = [SourceListItem(**x) for x in items]
        total_chunks = sum(x.count for x in source_items)
        return SourceListResponse(
            sources=source_items,
            total_sources=len(source_items),
            total_chunks=total_chunks,
        )
    except Exception as e:
        logger.exception(f"/admin/knowledge/sources 失败: {_admin_ctx(admin)}")
        raise HTTPException(status_code=500, detail=f"查询失败: {type(e).__name__}")


@router.delete(
    "/knowledge/source/{source}",
    response_model=DeleteResponse,
    summary="按来源删除",
    description="删除指定 source 的全部 chunk（Qdrant 真删 + MySQL 软删 status=0）。需 admin 权限。",
)
async def knowledge_delete_by_source(
    request: Request,
    source: str = Path(..., min_length=1, max_length=200, description="来源标识"),
    admin: User = Depends(require_admin),
):
    ip = _client_ip(request)
    ua = _user_agent(request)

    try:
        deleted = await asyncio.wait_for(
            asyncio.to_thread(k_delete_by_source, source),
            timeout=MGMT_TIMEOUT,
        )
        logger.info(
            f"/admin/knowledge/source/{source} deleted={deleted} {_admin_ctx(admin)}"
        )
        try_log_action(
            user=admin,
            action="delete_knowledge",
            target_type="knowledge",
            target_id=source,
            ip=ip,
            user_agent=ua,
            detail={"deleted_chunks": deleted},
        )
        return DeleteResponse(deleted=deleted, target=source)
    except ValueError as e:
        try_log_action(
            user=admin,
            action="delete_knowledge",
            target_type="knowledge",
            target_id=source,
            ip=ip,
            user_agent=ua,
            result="fail",
            error_msg=str(e)[:500],
        )
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(
            f"/admin/knowledge/source/{source} 失败: {_admin_ctx(admin)}"
        )
        try_log_action(
            user=admin,
            action="delete_knowledge",
            target_type="knowledge",
            target_id=source,
            ip=ip,
            user_agent=ua,
            result="fail",
            error_msg=f"{type(e).__name__}: {str(e)[:300]}",
        )
        raise HTTPException(status_code=500, detail=f"删除失败: {type(e).__name__}")


@router.delete(
    "/knowledge/points",
    response_model=DeleteResponse,
    summary="按 point_id 删除",
    description="按 chunk_id 列表批量删除（仅 Qdrant）。需 admin 权限。",
)
async def knowledge_delete_by_ids(
    request: DeleteByIdsRequest,
    admin: User = Depends(require_admin),
):
    try:
        deleted = await asyncio.wait_for(
            asyncio.to_thread(k_delete_by_ids, request.chunk_ids),
            timeout=MGMT_TIMEOUT,
        )
        logger.info(
            f"/admin/knowledge/points deleted={deleted}, "
            f"ids={len(request.chunk_ids)} {_admin_ctx(admin)}"
        )
        return DeleteResponse(deleted=deleted, target="by_ids")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"/admin/knowledge/points 失败: {_admin_ctx(admin)}")
        raise HTTPException(status_code=500, detail=f"删除失败: {type(e).__name__}")
