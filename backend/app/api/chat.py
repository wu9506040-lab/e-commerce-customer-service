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
import uuid
from typing import Generator, Optional, Tuple, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user_optional
from app.core.config import settings
from app.core.context import set_session_id, set_user_id  # M8
from app.models.user import User
from app.schemas.chat import ChatRequest, ResumeRequest
from app.services.audit_service import try_log_action
from app.services.behavior_monitor import behavior_monitor  # M11.5 P2
from app.services.chat.orchestrator import Synthesizer  # Sprint 3：从 services/synthesizer 切到 services/chat/orchestrator
from app.services.chat.prompt_assembler import _build_meta_contexts  # Sprint 3：原 services/synthesizer
from app.services.escalation_service import (  # M14 V3：转人工兜底
    EscalationReason,
    detect_handoff_keyword,
    get_escalation_service,
)
from app.services.metrics import metrics  # M8
from app.services.policy_service import PolicyService
# Sprint P2 / SSE Resume：stream checkpoint 写入/读取/清理
from app.services.redis_store import (
    STREAM_MAX_RESUME_TIMES,
    del_stream_checkpoint,
    get_stream_checkpoint,
    increment_resume_count,
    set_stream_checkpoint,
)
from app.services.session_service import (
    ANONYMOUS_USER_ID,
    append_exchange,
    generate_session_id,
    load_history_with_fallback,
    persist_to_mysql,
)
# P2 长程记忆：每轮 done 后异步更新 user_profiles（best-effort）
from app.services import profile_service
from app.services.guard import guard as input_guard
from app.services.intent_service import IntentService
from app.services.response_cache import get_cached_answer, put_cached_answer

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


def _sse_format(data: dict, seq: Optional[int] = None) -> str:
    """格式化为 SSE data 行（每条 event 以 \\n\\n 结束）

    Sprint P2 / SSE Resume：传 seq 时加 `id: {seq}\\n` 行（SSE 标准 Last-Event-ID 协议）。
    seq 为 None 时保持原格式（向后兼容 guard/cache 等不需要 resume 的路径）。
    """
    if seq is not None:
        return f"id: {seq}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk_text(text: str, size: int = 10) -> list[str]:
    """按字符切片，模拟 LLM token 流（前端打字机效果）
    中文按字符切（不拆字节），英文按 size 字符切
    """
    if not text:
        return []
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + size])
        i += size
    return chunks


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
        # M11.5 P2：异常行为监控（在最早期记一次，含被 guard 拦的）
        # — 不阻塞业务（Redis 异常放行），只告警
        behavior_monitor.record_request(
            ip=ip, user_id=user_id,
            sku=payload.sku, order_no=payload.order_no,
        )

        # M11：InputGuard 3 层防御（在最早期拦住垃圾/闲聊/重复，省 LLM token）
        guard_result = input_guard.check(payload.query, user_id)
        if not guard_result.allowed:
            # 黑名单静默不响应；其他走固定话术
            yield _sse_format({
                "type": "meta",
                "intent": "blocked",
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "contexts": [],
                "scores": [],
                "guard_layer": guard_result.layer,
                "guard_reason": guard_result.reason,
            })
            if guard_result.response:
                # 分段 yield，模拟正常 token 流（前端能正常渲染）
                for chunk in _chunk_text(guard_result.response, size=10):
                    yield _sse_format({"type": "token", "text": chunk})
            yield _sse_format({"type": "done", "session_id": session_id})
            yield _sse_format({"type": "closed"})
            try_log_action(
                user=user, action="chat_guard_blocked", target_type="session",
                target_id=session_id, ip=ip, user_agent=ua,
                detail={
                    "guard_layer": guard_result.layer,
                    "guard_reason": guard_result.reason,
                    "query_len": len(payload.query),
                },
            )
            logger.info(
                f"/chat guard blocked: layer={guard_result.layer} "
                f"reason={guard_result.reason} {user_ctx}"
            )
            return

        # M14 V3：转人工关键词检测（在 IntentService.classify 之前，跳过 LLM 调用）
        if detect_handoff_keyword(payload.query):
            escalation = get_escalation_service()
            handoff_payload = escalation.handoff(
                reason=EscalationReason.USER_REQUESTED,
                user_id=user_id,
                history=history,
                intent_result=None,
                failure_context=None,
            )
            yield _sse_format({
                "type": "meta",
                "intent": "handoff",
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "contexts": [],
                "scores": [],
                "handoff": handoff_payload.to_dict(),
            })
            for chunk in _chunk_text(
                f"{handoff_payload.reason_label}（工单号 {handoff_payload.handoff_id}），人工客服会尽快联系您～",
                size=10,
            ):
                yield _sse_format({"type": "token", "text": chunk})
            yield _sse_format({"type": "done", "session_id": session_id})
            yield _sse_format({"type": "closed"})
            try_log_action(
                user=user, action="chat_handoff_user_requested", target_type="session",
                target_id=session_id, ip=ip, user_agent=ua,
                detail={"handoff_id": handoff_payload.handoff_id},
            )
            logger.info(
                f"/chat handoff (user_requested): handoff_id={handoff_payload.handoff_id} "
                f"session={session_id[:12]}... {user_ctx}"
            )
            return

        # M13.1：refund_query 不进缓存（meta refundable/reason 是 order 相关的，不能复用）
        try:
            pre_intent = await asyncio.to_thread(IntentService.classify, payload.query)
            intent_for_cache = pre_intent.get("intent", "policy_query")
        except Exception:
            intent_for_cache = "policy_query"

        # M11.5：响应缓存（exact + semantic），10min 内同 query 不再调 LLM
        # 仅对 policy_query 启用，refund_query 必须走 LangGraph 保证 meta 正确
        cached_answer = None
        if intent_for_cache == "policy_query":
            cached_answer = await asyncio.to_thread(
                get_cached_answer, payload.query, user_id
            )
        if cached_answer:
            # policy_query 才进缓存（refund_query 已在前置分类跳过）
            cached_entities = pre_intent.get("entities", {"order_no": None, "sku": None, "keywords": []})
            # P0-H：cache_hit 也暴露检索 contexts（复用 PolicyService 检索，不调 LLM）
            # LLM 跳过是因为答案命中缓存，RAG 检索是 cheap 操作值得仍跑
            cache_contexts: list = []
            cache_scores: list = []
            try:
                cache_policy_docs = await asyncio.to_thread(
                    PolicyService.search_policy, payload.query, 5
                )
                cache_contexts, cache_scores = _build_meta_contexts(policy_docs=cache_policy_docs)
            except Exception as e:
                logger.warning(f"cache_hit 拿 contexts 失败（放行）: {e}")
            yield _sse_format({
                "type": "meta",
                "intent": intent_for_cache,
                "intent_method": "cache_hit",
                "entities": cached_entities,
                "policy_hits": len(cache_contexts),  # 实际 RAG 命中数，不是缓存标记
                "contexts": cache_contexts,
                "scores": cache_scores,
            })
            for chunk in _chunk_text(cached_answer, size=10):
                yield _sse_format({"type": "token", "text": chunk})
            yield _sse_format({"type": "done", "session_id": session_id})
            yield _sse_format({"type": "closed"})
            # 写穿透（best-effort，让历史能复用）
            try:
                await asyncio.to_thread(
                    append_exchange, session_id, payload.query, cached_answer
                )
            except Exception as e:
                logger.warning(f"cache 命中后写历史失败: {e}")
            try_log_action(
                user=user, action="chat_cache_hit", target_type="session",
                target_id=session_id, ip=ip, user_agent=ua,
                detail={"query_len": len(payload.query), "answer_len": len(cached_answer)},
            )
            logger.info(f"/chat cache hit: session={session_id[:12]}... {user_ctx}")
            return

        full_answer = ""
        contexts: list = []
        scores: list = []
        last_heartbeat = time.time()
        chat_start = time.perf_counter()  # M8：记录 chat 总耗时
        # Sprint P2 / SSE Resume：本回合 stream_id（前端拿到后用于 resume）
        # + seq 计数器（每个 SSE event 自增，写入 id: 行）
        stream_id = uuid.uuid4().hex[:12]
        seq = 0
        # 把 Synthesizer.run_stream 包装成 async iterator
        # （它原本是同步 generator，用 to_thread 异步化）
        from app.services.chat.orchestrator import Synthesizer as _S
        # M9.5：把 sku/order_no context 传给 synthesizer（让 LLM 知道当前商品/订单）
        # M14：传 session_id 让 OrderContextResolver 加载会话上下文
        sync_iter = iter(_S.run_stream(
            payload.query, user_id, history,
            sku=payload.sku, order_no=payload.order_no,
            session_id=session_id,
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
                    seq += 1
                    meta_payload = {**data, "stream_id": stream_id}
                    contexts = meta_payload.get("contexts", [])
                    scores = meta_payload.get("scores", [])
                    yield _sse_format({"type": "meta", **meta_payload}, seq=seq)
                elif event_type == "token":
                    seq += 1
                    full_answer += data
                    yield _sse_format({"type": "token", "text": data}, seq=seq)
                    # Sprint P2 / SSE Resume：异步写 checkpoint（fire-and-forget，
                    # 不阻塞流；CancelledError 块会同步兜底覆盖最终状态）
                    try:
                        asyncio.create_task(asyncio.to_thread(
                            set_stream_checkpoint,
                            session_id, stream_id, full_answer, seq, payload.query,
                        ))
                    except RuntimeError:
                        # 无 running loop（极端边界）→ 跳过；下次 token 会覆盖
                        pass
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
                    # M11.5：响应缓存（10min 内同 query 不调 LLM）
                    try:
                        await asyncio.to_thread(
                            put_cached_answer, payload.query, user_id, full_answer
                        )
                    except Exception as e:
                        logger.warning(f"缓存写入失败: {e}")
                    try:
                        await asyncio.to_thread(
                            persist_to_mysql,
                            session_id, user_id, payload.query, full_answer, contexts, scores,
                        )
                    except Exception as e:
                        logger.warning(
                            f"MySQL 写穿透失败: session={session_id[:12]}..., {e}"
                        )
                    # P2 长程记忆：每轮 done 后 best-effort 累加 interaction_count；
                    # 若 payload.sku 非空（用户从商品详情跳转），追加到 frequent_skus。
                    # 灰度开关：ENABLE_USER_PROFILE=False 时短路（不调 profile_service）
                    if settings.ENABLE_USER_PROFILE and user_id and user_id != ANONYMOUS_USER_ID:
                        try:
                            await asyncio.to_thread(
                                profile_service.increment_interaction, user_id, 1
                            )
                            if payload.sku:
                                await asyncio.to_thread(
                                    profile_service.append_frequent_skus,
                                    user_id,
                                    [payload.sku],
                                )
                        except Exception as e:
                            # profile 更新失败不影响 done 响应（best-effort）
                            logger.warning(
                                f"profile 更新失败（放行）: user_id={user_id}, {e}"
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

                    yield _sse_format({"type": "done", "session_id": session_id}, seq=seq + 1)
                    seq += 1
                    # Sprint P2 / SSE Resume：正常完成清理 checkpoint
                    try:
                        await asyncio.to_thread(
                            del_stream_checkpoint, session_id, stream_id
                        )
                    except Exception as e:
                        logger.warning(f"checkpoint 清理失败（TTL 自然过期兜底）: {e}")
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
            # Sprint P2 / SSE Resume：同步写最终 checkpoint（兜底覆盖异步 task 可能丢失的写入）
            # 同步调用，不 await，避免在 CancelledError 路径再次被取消
            try:
                set_stream_checkpoint(
                    session_id, stream_id, full_answer, seq, payload.query
                )
            except Exception as e:
                logger.warning(f"checkpoint 同步落盘失败: {e}")
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


# =============================================================
# POST /chat/resume - SSE 流式中断续传（Sprint P2 / SSE Resume）
# =============================================================
# 设计：
#   - 客户端在 /chat 流中断（未收到 done）后调用
#   - 后端从 Redis checkpoint 读取 prefix_text，重发到前端
#   - 不调 LLM 续写（用户拍板：checkpoint 重发即可，不强求"补全"）
#   - 同 (session_id, stream_id) 最多 resume STREAM_MAX_RESUME_TIMES 次
#   - 第 3 次断流 → 前端走普通重试（catch 后重新调 /chat）
@router.post(
    "/chat/resume",
    summary="SSE 流式中断续传（checkpoint 重发）",
    description=(
        "当 /chat 流中断且未收到 done 时调用。"
        "后端从 Redis checkpoint 读取 prefix_text 一次性重发，"
        "前端可立即渲染（用户视角无感）。"
        "同 (session_id, stream_id) 最多 2 次续传。"
    ),
    response_class=StreamingResponse,
)
async def chat_resume(
    request: Request,
    payload: ResumeRequest,
    user: Optional[User] = Depends(get_current_user_optional),
):
    """SSE 流式中断续传端点

    事件流：
        id: 1
        data: {"type":"resume_prefix","prefix_text":"...","from_event_id":42}\\n\\n

        id: 2
        data: {"type":"done","session_id":"..."}\\n\\n

        id: 3
        data: {"type":"closed"}\\n\\n

    异常路径（前置校验失败，返 410 / 404）：
        - checkpoint 不存在 / TTL 过期 → 410 Gone
        - query 不匹配 → 410 Gone（防 query mismatch 注入）
        - resume 次数超限 → 410 Gone
    """
    session_id = payload.session_id
    stream_id = payload.stream_id
    user_id = user.id if user else ANONYMOUS_USER_ID
    user_ctx = (
        f"user={user.username}(id={user.id})" if user else "user=anonymous"
    )

    # 1. 读 checkpoint（不存在或 TTL 过期 → 410）
    cp = await asyncio.to_thread(
        get_stream_checkpoint, session_id, stream_id
    )
    if not cp:
        logger.info(
            f"/chat/resume checkpoint miss: session={session_id[:12]}..., "
            f"stream_id={stream_id} {user_ctx}"
        )
        raise HTTPException(
            status_code=410,
            detail="checkpoint not found or expired",
        )

    # 2. query 校验（防 query mismatch 注入；用户拍板不缓存 query 无关前缀）
    if cp["query"] != payload.query:
        logger.warning(
            f"/chat/resume query mismatch: session={session_id[:12]}..., "
            f"stream_id={stream_id} {user_ctx}"
        )
        raise HTTPException(
            status_code=410,
            detail="query mismatch (resume 必须传相同的 query)",
        )

    # 3. 限流：先 GET 计数，超限直接 410（避免误扣）
    #    race window 极小（< 1ms），业务上可接受
    from app.clients.redis_client import get_client as _redis_get
    count_key = f"chat:stream:resume_count:{session_id}:{stream_id}"
    current_count_raw = await asyncio.to_thread(_redis_get().get, count_key)
    current_count = int(current_count_raw or 0)
    if current_count >= STREAM_MAX_RESUME_TIMES:
        logger.info(
            f"/chat/resume 限流: session={session_id[:12]}..., "
            f"stream_id={stream_id}, count={current_count} {user_ctx}"
        )
        raise HTTPException(
            status_code=410,
            detail=f"resume limit exceeded (max={STREAM_MAX_RESUME_TIMES})",
        )

    # 4. INCR 增加计数（在 GET 通过后；race 时最多多扣 1 次，可接受）
    await asyncio.to_thread(
        increment_resume_count, session_id, stream_id
    )

    logger.info(
        f"/chat/resume start: session={session_id[:12]}..., "
        f"stream_id={stream_id}, prefix_len={len(cp['prefix_text'])}, "
        f"count_after={current_count + 1} {user_ctx}"
    )

    # 5. ContextVar 注入（与 chat 端点对齐，便于日志追踪）
    set_session_id(session_id)
    set_user_id(user_id if user else None)

    # 6. async generator：resume_prefix → done → closed
    async def resume_event_generator():
        seq = 0
        try:
            # 客户端断开检测（resume 流极短，但保留健壮性）
            if await request.is_disconnected():
                logger.info(
                    f"/chat/resume 客户端提前断开: session={session_id[:12]}..., "
                    f"stream_id={stream_id} {user_ctx}"
                )
                return

            # 6.1 resume_prefix：一次性把已流 prefix 重发给前端
            seq += 1
            yield _sse_format(
                {
                    "type": "resume_prefix",
                    "prefix_text": cp["prefix_text"],
                    "from_event_id": int(cp["last_event_id"]),
                    "stream_id": stream_id,
                },
                seq=seq,
            )

            # 6.2 done（标记本回合完成）
            seq += 1
            yield _sse_format(
                {"type": "done", "session_id": session_id},
                seq=seq,
            )

            # 6.3 graceful close
            seq += 1
            yield _sse_format({"type": "closed"}, seq=seq)

            logger.info(
                f"/chat/resume done: session={session_id[:12]}..., "
                f"stream_id={stream_id}, prefix_len={len(cp['prefix_text'])} {user_ctx}"
            )

        except asyncio.CancelledError:
            # resume 流极短，CancelledError 概率极低；保留防御性日志
            logger.info(
                f"/chat/resume CancelledError: session={session_id[:12]}..., "
                f"stream_id={stream_id} {user_ctx}"
            )
            raise

    return StreamingResponse(
        resume_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )