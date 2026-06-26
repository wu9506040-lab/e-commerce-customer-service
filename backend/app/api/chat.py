"""
Chat HTTP 接口层（RAG + 多轮对话 + MySQL write-through §11 + 流式输出 §14）

按 §6 规则：
- api/ 只负责路由 + 参数解析 + 调 services
- 不写业务逻辑（RAG 编排全在 services/rag/pipeline.py）

§10 起：可选 user 上下文
§11 起：write-through Redis + MySQL，audit 上报
§14 起：SSE 流式输出（POST /chat 升级为 text/event-stream）

实现：
    POST /chat → load_history_with_fallback → pipeline.run_stream(query, history)
              → 边收 token 边 yield SSE → 收 done 后 write-through（Redis + MySQL + audit）
"""
import asyncio
import json
import logging
from typing import Generator, Optional, Tuple, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user_optional
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services.audit_service import try_log_action
from app.services.rag.pipeline import run_stream as rag_run_stream
from app.services.session_service import (
    ANONYMOUS_USER_ID,
    append_exchange,
    generate_session_id,
    load_history_with_fallback,
    persist_to_mysql,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["chat"])


def _client_ip(request: Request) -> Optional[str]:
    """取客户端 IP（优先 X-Forwarded-For，再 client.host）"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _user_agent(request: Request) -> Optional[str]:
    """取 UA，截断 500 字符"""
    ua = request.headers.get("user-agent", "")
    return ua[:500] if ua else None


def _sse_format(data: dict) -> str:
    """格式化为 SSE data 行（每条 event 以 \\n\\n 结束）"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# =============================================================
# POST /chat - SSE 流式版本（§14）
# =============================================================
@router.post(
    "/chat",
    summary="RAG 多轮问答（SSE 流式）",
    description=(
        "基于知识库的检索增强问答，支持多轮会话。"
        "返回 text/event-stream，事件类型：meta / token / done / error。"
        "需要 httpOnly Cookie 鉴权（自动通过浏览器携带）。"
    ),
    response_class=StreamingResponse,
)
async def chat(
    request: Request,
    payload: ChatRequest,
    user: Optional[User] = Depends(get_current_user_optional),
):
    """
    RAG Chat 端点（流式版本）

    SSE 协议（自定义 JSON 事件）：
        data: {"type":"meta","contexts":[...],"scores":[...]}\\n\\n
        data: {"type":"token","text":"你"}\\n\\n
        data: {"type":"token","text":"好"}\\n\\n
        ...
        data: {"type":"done","session_id":"..."}\\n\\n

    或错误：
        data: {"type":"error","message":"..."}\\n\\n

    写入策略：
        - meta/token 实时 yield 给客户端
        - done 时再 write-through（Redis + MySQL + audit）
        - write-through 失败不影响 done 事件（best-effort）
    """
    # 1. 决定 session_id（缺失则新建）
    session_id = payload.session_id or generate_session_id()

    user_id = user.id if user else ANONYMOUS_USER_ID
    user_ctx = (
        f"user={user.username}(id={user.id})" if user else "user=anonymous"
    )
    ip = _client_ip(request)
    ua = _user_agent(request)

    # 2. 预加载历史（同步 IO，asyncio.to_thread 异步化）
    #    历史加载失败 → 直接 500，不进入流式（流必须从完整上下文开始）
    try:
        history = await asyncio.to_thread(
            load_history_with_fallback, session_id
        )
    except Exception as e:
        logger.exception(f"/chat history 加载失败: session={session_id[:12]}... {user_ctx}")
        raise HTTPException(status_code=500, detail=f"历史加载失败: {type(e).__name__}")

    logger.info(
        f"/chat start: session={session_id[:12]}..., "
        f"history_len={len(history)} {user_ctx}"
    )

    # 3. 构造 SSE 事件生成器（同步 generator，FastAPI 在 threadpool 里迭代）
    def event_generator() -> Generator[str, None, None]:
        full_answer = ""
        contexts: list = []
        scores: list = []
        error_msg: Optional[str] = None

        try:
            for event_type, data in rag_run_stream(payload.query, 5, history):
                if event_type == "meta":
                    contexts = data["contexts"]
                    scores = data["scores"]
                    yield _sse_format({
                        "type": "meta",
                        "contexts": contexts,
                        "scores": scores,
                    })
                elif event_type == "token":
                    full_answer += data
                    yield _sse_format({"type": "token", "text": data})
                elif event_type == "done":
                    # 4. write-through（best-effort，失败仅 warning）
                    #    Redis 热路径
                    try:
                        append_exchange(session_id, payload.query, full_answer)
                    except Exception as e:
                        logger.warning(
                            f"Redis 写穿透失败: session={session_id[:12]}..., {e}"
                        )
                    #    MySQL 冷路径
                    try:
                        persist_to_mysql(
                            session_id,
                            user_id,
                            payload.query,
                            full_answer,
                            contexts,
                            scores,
                        )
                    except Exception as e:
                        logger.warning(
                            f"MySQL 写穿透失败: session={session_id[:12]}..., {e}"
                        )
                    #    audit 上报
                    try_log_action(
                        user=user,
                        action="chat",
                        target_type="session",
                        target_id=session_id,
                        ip=ip,
                        user_agent=ua,
                        detail={
                            "query_len": len(payload.query),
                            "answer_len": len(full_answer),
                            "hits": len(contexts),
                            "stream": True,
                        },
                    )

                    yield _sse_format({"type": "done", "session_id": session_id})

                    logger.info(
                        f"/chat stream done: session={session_id[:12]}..., "
                        f"answer_len={len(full_answer)}, hits={len(contexts)} {user_ctx}"
                    )
        except ValueError as e:
            error_msg = str(e)
            logger.error(f"/chat stream 参数错误: {error_msg} {user_ctx}")
            yield _sse_format({"type": "error", "message": error_msg})
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)[:200]}"
            logger.exception(f"/chat stream 调用失败: session={session_id[:12]}... {user_ctx}")
            yield _sse_format({"type": "error", "message": error_msg})

    # 5. 返回 StreamingResponse
    #    注意：X-Accel-Buffering: no 是给 nginx 看的，禁用 proxy buffering
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )