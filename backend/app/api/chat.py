"""
Chat HTTP 接口层（多源融合 + 多轮对话 + MySQL write-through §11 + 流式输出 §14）

按 §6 规则：
- api/ 只负责路由 + 参数解析 + 调 services
- 不写业务逻辑（M4 起编排全在 services/synthesizer.py）

§10 起：可选 user 上下文
§11 起：write-through Redis + MySQL，audit 上报
§14 起：SSE 流式输出（POST /chat 升级为 text/event-stream）
M4 起：Synthesizer.run_stream 替代 V1.2 统一 RAG，按意图分派到不同 service/tool
M7 起：SSE heartbeat + 客户端断开检测（健壮性加固）

实现：
    POST /chat → load_history_with_fallback → Synthesizer.run_stream(query, user_id, history)
              → 边收 token 边 yield SSE → 收 done 后 write-through（Redis + MySQL + audit）
              → 全程 heartbeat 保活（30s 间隔）+ 断开检测
"""
import asyncio
import json
import logging
import time
from typing import Generator, Optional, Tuple, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user_optional
from app.core.context import set_session_id, set_user_id  # M8
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services.audit_service import try_log_action
from app.services.metrics import metrics  # M8
from app.services.session_service import (
    ANONYMOUS_USER_ID,
    append_exchange,
    generate_session_id,
    load_history_with_fallback,
    persist_to_mysql,
)
from app.services.synthesizer import Synthesizer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# M7：heartbeat 间隔（秒）—— 30s 是 nginx 默认 proxy_read_timeout（60s）的一半
SSE_HEARTBEAT_INTERVAL = 30.0


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
# POST /chat - SSE 流式版本（§14 + M7 heartbeat）
# =============================================================
@router.post(
    "/chat",
    summary="RAG 多轮问答（SSE 流式）",
    description=(
        "基于知识库的检索增强问答，支持多轮会话。"
        "返回 text/event-stream，事件类型：meta / token / heartbeat / done / error / closed。"
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
        ...
        data: {"type":"heartbeat","ts":1234567890}\\n\\n   ← M7：30s 间隔
        data: {"type":"done","session_id":"..."}\\n\\n
        data: {"type":"closed"}\n\n                            ← M7：服务端优雅关闭

    健壮性（M7）：
        - heartbeat 事件：每 30s 发送，告知客户端"连接还活着"
        - 客户端断开检测：asyncio.CancelledError → 写审计 + 跳出循环
        - graceful close：正常结束时发 closed 事件（前端可识别）
    """
    # 1. 决定 session_id（缺失则新建）
    session_id = payload.session_id or generate_session_id()

    user_id = user.id if user else ANONYMOUS_USER_ID
    user_ctx = (
        f"user={user.username}(id={user.id})" if user else "user=anonymous"
    )
    ip = _client_ip(request)
    ua = _user_agent(request)

    # M8：把 session_id / user_id 写入 ContextVar（日志自动带）
    set_session_id(session_id)
    set_user_id(user_id if user else None)

    # 2. 预加载历史（同步 IO，asyncio.to_thread 异步化）
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

    # 3. 异步 SSE 生成器（M7：async generator + heartbeat + 断开检测）
    async def event_generator():
        full_answer = ""
        contexts: list = []
        scores: list = []
        last_heartbeat = time.time()
        chat_start = time.perf_counter()  # M8：记录 chat 总耗时
        # 把 Synthesizer.run_stream 包装成 async iterator
        # （它原本是同步 generator，用 to_thread 异步化）
        from app.services.synthesizer import Synthesizer as _S
        # M9.5：把 sku/order_no context 传给 synthesizer（让 LLM 知道当前商品/订单）
        sync_iter = iter(_S.run_stream(
            payload.query, user_id, history,
            sku=payload.sku, order_no=payload.order_no,
        ))

        try:
            while True:
                # 检查客户端是否断开（FastAPI 在 disconnect 时会 raise CancelledError）
                if await request.is_disconnected():
                    logger.info(
                        f"/chat 客户端断开: session={session_id[:12]}..., "
                        f"answer_so_far={len(full_answer)} {user_ctx}"
                    )
                    break

                # heartbeat 节流：每 30s 发一次（在等 LLM token 时穿插）
                now = time.time()
                if now - last_heartbeat >= SSE_HEARTBEAT_INTERVAL:
                    yield _sse_format({"type": "heartbeat", "ts": int(now * 1000)})
                    last_heartbeat = now

                # 拉取下一个事件（用 to_thread 不阻塞事件循环）
                try:
                    item = await asyncio.wait_for(
                        asyncio.to_thread(next, sync_iter, None),
                        timeout=SSE_HEARTBEAT_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    # 等不到下一个 token，发送 heartbeat 后继续等
                    yield _sse_format({"type": "heartbeat", "ts": int(time.time() * 1000)})
                    last_heartbeat = time.time()
                    continue
                except StopIteration:
                    break

                if item is None:  # sentinel: 同步迭代器耗尽
                    break

                event_type, data = item

                if event_type == "meta":
                    meta_payload = {**data}
                    contexts = meta_payload.get("contexts", [])
                    scores = meta_payload.get("scores", [])
                    yield _sse_format({"type": "meta", **meta_payload})
                elif event_type == "token":
                    full_answer += data
                    yield _sse_format({"type": "token", "text": data})
                elif event_type == "done":
                    # write-through（best-effort）
                    try:
                        await asyncio.to_thread(
                            append_exchange, session_id, payload.query, full_answer
                        )
                    except Exception as e:
                        logger.warning(
                            f"Redis 写穿透失败: session={session_id[:12]}..., {e}"
                        )
                    try:
                        await asyncio.to_thread(
                            persist_to_mysql,
                            session_id, user_id, payload.query, full_answer, contexts, scores,
                        )
                    except Exception as e:
                        logger.warning(
                            f"MySQL 写穿透失败: session={session_id[:12]}..., {e}"
                        )
                    try_log_action(
                        user=user, action="chat", target_type="session",
                        target_id=session_id, ip=ip, user_agent=ua,
                        detail={
                            "query_len": len(payload.query),
                            "answer_len": len(full_answer),
                            "hits": len(contexts),
                            "stream": True,
                        },
                    )

                    yield _sse_format({"type": "done", "session_id": session_id})
                    # M8：记录 chat 延迟（流式总耗时）
                    metrics.record_chat_latency(
                        round((time.perf_counter() - chat_start) * 1000, 1)
                    )
                    logger.info(
                        f"/chat stream done: session={session_id[:12]}..., "
                        f"answer_len={len(full_answer)}, hits={len(contexts)} {user_ctx}"
                    )

            # 4. graceful close（M7）
            yield _sse_format({"type": "closed"})

        except asyncio.CancelledError:
            # 客户端主动断开（FastAPI 内部触发）
            logger.info(
                f"/chat 客户端取消（CancelledError）: "
                f"session={session_id[:12]}..., answer_so_far={len(full_answer)} {user_ctx}"
            )
            # 不再 yield（连接已断，yield 无效）
            raise
        except ValueError as e:
            error_msg = str(e)
            logger.error(f"/chat stream 参数错误: {error_msg} {user_ctx}")
            yield _sse_format({"type": "error", "message": error_msg})
            yield _sse_format({"type": "closed"})
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)[:200]}"
            logger.exception(
                f"/chat stream 调用失败: session={session_id[:12]}... {user_ctx}"
            )
            yield _sse_format({"type": "error", "message": error_msg})
            yield _sse_format({"type": "closed"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
    # M8：ContextVar 是 per-task，generator 跑在同一 task 内无需 reset
    # （下一个请求会覆盖 set）