"""
Orchestrator（Sprint 3 拆分自 synthesizer.py）

职责：intent 分派 + 业务编排（chat handler 是"万能模块"转"调度器"）。
- run_stream 主入口
- _try_direct_answer_order 直答兜底
- _handle_order / _handle_product / _handle_policy
- _DIRECT_ANSWER_PATTERNS 直答关键词
- 分派异常 → fallback 到 V1.2 统一 RAG
- refund_query 委托 chat.refund_handler.handle_refund_v2 / handle_refund_v3

边界：不构造 prompt（委托 prompt_assembler）；不做 LLM 流式（委托 stream_dispatcher）；
refund 双轨制 V2/V3 逻辑在 chat.refund_handler。

范围引用：
- 系统 Prompt：经由 prompt_assembler.SYSTEM_PROMPT_BASE（最终走 prompt_loader）
- LLM 调用：经由 stream_dispatcher.stream_llm
- 简单文本：经由 stream_dispatcher.stream_simple
- 滑动窗口：经由 stream_dispatcher.search_by_keyword_window
- refund_query 委托：经由 chat.refund_handler
- intent 分派：本模块完成
"""
import logging
import re as _re
from typing import Any, Generator, Optional, Tuple

from app.core.config import settings
from app.services.context import (
    ConversationContext,
    ContextService,
    OrderContextResolver,
    OrderResolverAction,
    OrderResolverResult,
    get_context_service,
    get_order_context_resolver,
)
from app.services.intent_service import IntentService
from app.services.metrics import metrics
from app.services.order_service import OrderService
from app.services.policy_service import PolicyService
from app.services.rag.pipeline import run_stream as v12_rag_run_stream
from app.services.query_rewriter import rewrite_query  # M12：指代补全
from app.services.query_rewriter import rewrite_query_multi  # Phase 4 A4：Multi-Query 多路改写
from app.services.session_service import ANONYMOUS_USER_ID
from app.tools.product_tool import ProductTool

# P2 长程记忆：用户画像注入（默认 ENABLE_USER_PROFILE=False 灰度）
from app.services import profile_service

from app.services.chat import prompt_assembler, stream_dispatcher
from app.services.chat.refund_handler import handle_refund_v2, handle_refund_v3
# M14 Stage 5：audit 上报（5 个 resolver action 路径全留痕）
from app.services.audit_service import try_log_action
# M14 Stage 3：BusinessFlow 抽象（仅 refund_query 接入；其他 intent 暂不抽象）
from app.services.business_flow import create_business_flow

logger = logging.getLogger(__name__)


class Synthesizer:
    """多源融合层（M4）— Sprint 3 拆分后，orchestrator 仅负责调度"""

    @staticmethod
    def run_stream(
        query: str,
        user_id: Optional[int],
        history: Optional[list[dict]] = None,
        sku: Optional[str] = None,
        order_no: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Generator[Tuple[str, Any], None, None]:
        """
        主入口：分类 → 分派 → 融合 → LLM 流式输出

        Args:
            query: 用户问题
            user_id: 用户 ID（未登录为 ANONYMOUS_USER_ID = 0）
            history: 多轮历史 [{"role":..., "content":...}]
            sku: 当前商品 SKU（M9.5：从 /shop/:sku 跳转携带，注入 prompt 让 LLM 知道是哪款）
            order_no: 当前订单号（M9.5：从 OrderCard 跳转携带，注入 prompt 让 LLM 知道是哪个订单）
            session_id: 会话 ID（M14：OrderContextResolver 用其加载会话上下文）

        Yields:
            ("meta", {intent, entities, ...})
            ("token", str)
            ("done", {"answer": str})
        """
        if not query or not query.strip():
            raise ValueError("query 不能为空")
        query = query.strip()

        # C2：Agent Function Calling 灰度入口（ENABLE_AGENT_FC=True 时）
        # FC 路径独立：不走 query rewrite / profile / intent 分派；
        # 异常自包 try/except fallback 到 V1.2 RAG（与既有 fallback 路径一致）
        if settings.ENABLE_AGENT_FC:
            from app.services.chat.agent_runner import run_stream_agent
            try:
                yield from run_stream_agent(query, user_id=user_id, history=history)
                return
            except Exception as e:
                logger.exception(
                    f"synth.agent_fc 异常，fallback 到 V1.2 RAG: {e}",
                    extra={"intent": "agent_fc_error"},
                )
                for event_type, data in v12_rag_run_stream(query, 5, history):
                    yield (event_type, data)
                return

        # M12：query 改写（指代补全）— 改写后的 query 供后续 intent + RAG 使用
        # product_query/policy_query 走 PolicyService.search_policy → 改写有效
        # order_query/refund_query 走 tool 查 DB → 改写无效但无害
        rewritten_query, was_rewritten = rewrite_query(query, history)
        if was_rewritten:
            logger.info(
                f"synth.rewritten: orig='{query[:40]}...' "
                f"new='{rewritten_query[:40]}...' user_id={user_id}",
                extra={"intent": "rewritten"},
            )
            query = rewritten_query

        # Phase 4 A4：Multi-Query 多路改写（仅 policy/product 有效；ENABLE_MULTI_QUERY 默认 false）
        # 触发条件由 query_rewriter.MULTI_QUERY_TRIGGER 管控（默认 coref_only）
        # 失败 / 无历史 / LLM 异常 → rewrite_query_multi 降级返 [query] → 等价单路
        search_queries: Optional[list[str]] = None
        if settings.ENABLE_MULTI_QUERY:
            multi_queries, _ = rewrite_query_multi(query, history)
            if multi_queries and len(multi_queries) > 1:
                search_queries = multi_queries
                logger.info(
                    f"synth.multi_query: n={len(multi_queries)} "
                    f"first='{multi_queries[0][:40]}...' user_id={user_id}",
                    extra={"intent": "multi_query"},
                )

        # M9.5：预加载 context（商品/订单详情），后续注入 LLM prompt
        # P2 长程记忆：扩 profile_block（来自 profile_service.to_prompt_block）
        #   灰度开关：settings.ENABLE_USER_PROFILE=False → profile_block 始终空串（短路）
        #   匿名用户（user_id=ANONYMOUS_USER_ID）→ 不维护 profile
        profile_block = ""
        if settings.ENABLE_USER_PROFILE and user_id and user_id != ANONYMOUS_USER_ID:
            try:
                profile = profile_service.get_or_create(user_id)
                profile_block = profile_service.to_prompt_block(
                    profile, max_len=settings.USER_PROFILE_PROMPT_MAX_LEN
                )
            except Exception as e:
                # best-effort：profile 加载失败不阻塞主流程
                logger.warning(
                    f"profile 加载失败（放行）: user_id={user_id}, {e}",
                    extra={"intent": "profile_error"},
                )
                profile_block = ""

        context_block = prompt_assembler._build_context_block(
            sku, order_no, user_id, profile_block=profile_block
        )
        if context_block:
            logger.info(
                f"synth.context: sku={sku} order_no={order_no} "
                f"context_len={len(context_block)} profile_len={len(profile_block)} "
                f"user_id={user_id}",
                extra={"intent": "context"},
            )

        # 1. 意图分类
        intent_result = IntentService.classify(query)
        intent = intent_result["intent"]
        entities = intent_result["entities"]
        # M8：intent 用 extra 显式带（避免 ContextVar 跨 thread context 不可 reset 的问题）
        logger.info(
            f"synth.start: intent={intent} method={intent_result['method']} "
            f"conf={intent_result['confidence']:.2f} user_id={user_id}",
            extra={"intent": intent},
        )

        # 2. 分派（按 intent 调用对应 service/tool）
        try:
            if intent == "order_query":
                metrics.inc_chat(intent, v3_engine="-")  # M8
                # M9.5：传 order_no 让 order_query 优先用跳转来的订单
                # M14：传 session_id 让 OrderContextResolver 加载会话上下文
                yield from Synthesizer._handle_order(
                    query, user_id, intent_result,
                    order_no=order_no, context_block=context_block,
                    session_id=session_id,
                )
                return
            elif intent == "refund_query":
                # M14 Stage 3：BusinessFlow 抽象（灰度 ENABLE_BUSINESS_FLOW=True 时走 RefundFlow）
                # RefundFlow 包装 V3 LangGraph + yield flow_stage meta；与 handle_refund_v3 行为兼容
                flow = create_business_flow(
                    intent="refund_query",
                    query=query,
                    user_id=user_id,
                    intent_result=intent_result,
                    order_no=order_no,
                    context_block=context_block,
                    history=history,
                )
                if flow is not None:
                    logger.info(
                        f"refund_query → {flow.name}",
                        extra={"intent": intent, "flow": flow.name},
                    )
                    metrics.inc_chat(intent, v3_engine="flow")  # M8：Flow 路径独立计数
                    yield from flow.run()
                    return
                # 灰度关闭 / 不参与 Flow 抽象 → 走原有 V3/V2 路径
                if settings.USE_LANGGRAPH_REFUND:
                    logger.info("refund_query → LangGraph V3", extra={"intent": intent})
                    metrics.inc_chat(intent, v3_engine="v3")  # M8
                    yield from handle_refund_v3(query, user_id, intent_result, order_no=order_no, context_block=context_block, history=history)
                else:
                    metrics.inc_chat(intent, v3_engine="v2")  # M8
                    yield from handle_refund_v2(query, user_id, intent_result, order_no=order_no, context_block=context_block)
                return
            elif intent == "product_query":
                metrics.inc_chat(intent, v3_engine="-")  # M8
                yield from Synthesizer._handle_product(query, intent_result, history, sku=sku, context_block=context_block, search_queries=search_queries)
                return
            else:  # policy_query
                metrics.inc_chat(intent, v3_engine="-")  # M8
                yield from Synthesizer._handle_policy(query, intent_result, history, context_block=context_block, search_queries=search_queries)
                return
        except Exception as e:
            # 任何分派路径异常 → fallback 到 V1.2 统一 RAG
            logger.exception(
                f"synth.dispatch 异常，fallback 到 V1.2 RAG: intent={intent}, err={e}",
                extra={"intent": intent},
            )
            # 注意：fallback 不带 user_id（V1.2 pipeline 不接收 user_id）
            for event_type, data in v12_rag_run_stream(query, 5, history):
                yield (event_type, data)

    # ---------- 各 intent 分派实现 ----------

    # M11.5：直答关键词（命中即工具直答，不调 LLM）
    _DIRECT_ANSWER_PATTERNS = {
        "order_status": _re.compile(
            r"什么状态|到哪了|在哪|到了没|进度|物流到|快递到|发货了没|出库了没|派送中吗|签收了吗|"
            r"什么进度|到货了吗|发了吗|发出去了吗|派送了吗"
        ),
        "policy_simple": _re.compile(
            r"^.{0,15}(怎么退|怎么换|运费多少|几天到|什么时候发货|发票怎么开|保多久|保修期|"
            r"怎么开发票|能开发票|有发票吗|能退吗|几天能到|包邮吗|包邮不|发什么快递|发顺丰吗|发京东吗|"
            r"有什么颜色|什么颜色|有现货吗|有货吗)$"
        ),
    }

    @staticmethod
    def _try_direct_answer_order(
        query: str, user_id: int, entities: dict,
        order_no: Optional[str],
    ) -> Optional[str]:
        """M11.5：order_query 工具直答（命中模式即返模板，不调 LLM）

        Returns:
            直答文本；非直答场景返 None（走 LLM 综合）
        """
        effective_order_no = order_no or entities.get("order_no")
        if not effective_order_no or user_id == ANONYMOUS_USER_ID:
            return None
        # 模式匹配
        if not Synthesizer._DIRECT_ANSWER_PATTERNS["order_status"].search(query):
            return None
        # 查订单
        detail = OrderService.get_order_detail(user_id, effective_order_no)
        if not detail:
            return f"订单 {effective_order_no} 不存在或不属于当前用户。"

        order = detail.get("order", {})
        items = detail.get("items", [])
        logi = detail.get("logistics") or {}
        status = order.get("status", "未知")
        amount = order.get("total_amount", 0)
        create_time = (order.get("create_time") or "")[:10]

        # 状态中文
        _STATUS = {
            "pending":   "待支付",
            "paid":      "已支付，待发货",
            "shipped":   "运输中",
            "delivered": "已签收",
            "completed": "已完成",
            "refunded":  "已退款",
        }
        status_zh = _STATUS.get(status, status)

        # 拼直答
        lines = [
            f"订单 {effective_order_no} 当前状态：{status_zh}。",
            f"下单时间：{create_time}，金额：¥{float(amount):.2f}。",
        ]
        if items:
            item_text = "、".join(f"{it['product_name']}×{it['qty']}" for it in items[:3])
            if len(items) > 3:
                item_text += f" 等{len(items)}件"
            lines.append(f"商品：{item_text}。")
        if logi:
            logi_no = logi.get("logistics_no") or "—"
            logi_status = logi.get("status", "—")
            last_loc = logi.get("last_location") or "—"
            lines.append(f"物流：{logi_no} | {logi_status} | 最近位置 {last_loc}。")
        return "\n".join(lines)

    @staticmethod
    def _handle_order(
        query: str, user_id: int, intent_result: dict,
        order_no: Optional[str] = None,
        context_block: str = "",
        session_id: Optional[str] = None,
    ) -> Generator[Tuple[str, Any], None, None]:
        """order_query：M14 接入 OrderContextResolver 做 0/1/N 决策

        数据流：
        1. 加载 ConversationContext（last_intent / current_order_no / flow_state）
        2. 走 OrderContextResolver 决策（灰度 ENABLE_ORDER_RESOLVER 控制）
        3. 按 action 分派：
           - DIRECT_ANSWER → 查详情 / 调 LLM
           - SHOW_PICKER → 推送 OrderCard list 卡片 + 询问
           - NOT_FOUND → 报错文本
           - ASK_LOGIN / ASK_LOGIN_OR_LIST → 兜底话术
        """
        entities = intent_result["entities"]
        # M9.5：优先用 context 传来的 order_no（用户从订单卡片跳转），其次 intent 抽取
        effective_order_no = order_no or entities.get("order_no")

        # M14：早期加载会话上下文（last_intent / current_order_no），供 Resolver 使用
        # 灰度 ENABLE_CONTEXT_STORE=False 时 load 返空 ConversationContext
        ctx = (
            get_context_service().load(session_id, user_id)
            if session_id else ConversationContext(session_id="", user_id=user_id)
        )

        # 匿名用户优先 short-circuit（保持原行为不变）
        if user_id == ANONYMOUS_USER_ID:
            yield ("meta", {
                "intent": "order_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
            })
            yield from stream_dispatcher.stream_simple(prompt_assembler.NO_LOGIN_PROMPT)
            return

        # M14：OrderContextResolver 决策（灰度关闭时返 DIRECT_ANSWER，走老路径）
        resolver = get_order_context_resolver()
        result: OrderResolverResult = resolver.resolve(
            user_id=user_id,
            intent=intent_result["intent"],
            entities={**entities, "order_no": effective_order_no},
            ctx=ctx,
        )

        # M11.5：DIRECT_ANSWER 且满足"什么状态"类模式 → 工具直答（不调 LLM）
        # 注意：Resolver 已校验有效 order_no 后才走这里，所以 effective_order_no 可信
        if result.action == OrderResolverAction.DIRECT_ANSWER:
            direct = Synthesizer._try_direct_answer_order(
                query, user_id, entities, result.effective_order_no
            )
            if direct is not None:
                meta = Synthesizer._build_order_meta(
                    intent_result=intent_result,
                    detail=None,
                    direct_answer=True,
                    card=None,
                )
                yield ("meta", meta)
                yield from stream_dispatcher.stream_simple(direct)
                # M14：埋点（DIRECT_ANSWER 直答，card 不发）
                metrics.inc_resolver_decision(
                    action=result.action.value,
                    total_orders=result.total_orders,
                    card_sent=False,
                    card_expected=False,
                )
                # M14 Stage 5：audit 留痕（5 个 action 路径通用；orchestrator 无 User/IP，故 user=None）
                try_log_action(
                    user=None, action="resolver_decision",
                    target_type="user", target_id=str(user_id),
                    detail={
                        "intent": "order_query",
                        "resolver_action": result.action.value,
                        "resolver_reason": result.reason,
                        "total_orders": result.total_orders,
                        "card_sent": False,
                        "direct_answer": True,
                        "used_llm": False,
                        "truncated": result.truncated,
                    },
                )
                return

        # M14：按 Resolver action 分派
        if result.action == OrderResolverAction.SHOW_PICKER:
            # N 个订单 → 推 OrderCard list 卡片 + 让用户选
            card = Synthesizer._build_order_card_payload(
                result, density="list", reason="disambiguate"
            )
            meta = Synthesizer._build_order_meta(
                intent_result=intent_result,
                detail=None,
                direct_answer=False,
                card=card,
                resolver_result=result,
            )
            yield ("meta", meta)
            text = (
                f"您有 {result.total_orders} 个相关订单，请选择要查询的订单："
                if not result.truncated
                else f"您最近订单较多（{result.total_orders} 个），以下仅展示前 {len(card['items'])} 个："
            )
            yield from stream_dispatcher.stream_simple(text)
            # M14：埋点（card 实际发送 + 期望发送都 True）
            metrics.inc_resolver_decision(
                action=result.action.value,
                total_orders=result.total_orders,
                card_sent=True,
                card_expected=True,
            )
            # M14 Stage 5：audit 留痕
            try_log_action(
                user=None, action="resolver_decision",
                target_type="user", target_id=str(user_id),
                detail={
                    "intent": "order_query",
                    "resolver_action": result.action.value,
                    "resolver_reason": result.reason,
                    "total_orders": result.total_orders,
                    "card_sent": True,
                    "card_density": card.get("density") if card else None,
                    "card_type": card.get("type") if card else None,
                    "truncated": result.truncated,
                },
            )
            return

        if result.action == OrderResolverAction.NOT_FOUND:
            # order_no 无效 / 不属于当前用户
            meta = Synthesizer._build_order_meta(
                intent_result=intent_result,
                detail=None,
                direct_answer=False,
                card=None,
                resolver_result=result,
            )
            yield ("meta", meta)
            yield from stream_dispatcher.stream_simple(
                f"订单 {result.effective_order_no} 不存在或不属于当前用户。"
            )
            # M14：埋点（card 不应发 / 实际未发）
            metrics.inc_resolver_decision(
                action=result.action.value,
                total_orders=result.total_orders,
                card_sent=False,
                card_expected=False,
            )
            # M14 Stage 5：audit 留痕
            try_log_action(
                user=None, action="resolver_decision",
                target_type="user", target_id=str(user_id),
                detail={
                    "intent": "order_query",
                    "resolver_action": result.action.value,
                    "resolver_reason": result.reason,
                    "total_orders": result.total_orders,
                    "card_sent": False,
                    "invalid_order_no": result.effective_order_no,
                },
            )
            return

        if result.action == OrderResolverAction.ASK_LOGIN_OR_LIST:
            # 0 订单
            meta = Synthesizer._build_order_meta(
                intent_result=intent_result,
                detail=None,
                direct_answer=False,
                card=None,
                resolver_result=result,
            )
            yield ("meta", meta)
            yield from stream_dispatcher.stream_simple(
                "您当前还没有订单记录哦～ 如果刚下单，可能还未同步，请稍后再试。"
            )
            # M14：埋点（card 不应发：0 订单无候选）
            metrics.inc_resolver_decision(
                action=result.action.value,
                total_orders=0,
                card_sent=False,
                card_expected=False,
            )
            # M14 Stage 5：audit 留痕
            try_log_action(
                user=None, action="resolver_decision",
                target_type="user", target_id=str(user_id),
                detail={
                    "intent": "order_query",
                    "resolver_action": result.action.value,
                    "resolver_reason": result.reason,
                    "total_orders": 0,
                    "card_sent": False,
                },
            )
            return

        # DIRECT_ANSWER 路径（含 Resolver 关闭的 fallback / 唯一 1 单 / 用户提供有效 order_no）
        # 用 Resolver 选的 effective_order_no（可能与 intent 抽取的不同）
        final_order_no = result.effective_order_no or effective_order_no
        if final_order_no:
            detail = OrderService.get_order_detail(user_id, final_order_no)
            if not detail:
                tool_block = f"订单 {final_order_no} 不存在或不属于当前用户。"
                contexts, scores = [], []
            else:
                tool_block = prompt_assembler._format_tool_result("order_query", detail)
                contexts, scores = prompt_assembler._build_meta_contexts(tool_result=detail)
            card = None
            if result.total_orders == 1 and settings.SSE_CARD_V2:
                # 唯一 1 单 → 详情卡 mini
                card = Synthesizer._build_order_card_payload(
                    result, density="mini", reason="context_jump"
                )
        else:
            # 兜底（理论上 Resolver 已覆盖 0/1/N；保留以防灰度关闭 + 老逻辑兼容）
            orders = OrderService.list_user_orders(user_id)
            tool_block = prompt_assembler._format_tool_result("order_query", {"orders": orders})
            contexts, scores = prompt_assembler._build_meta_contexts(tool_result={"orders": orders})
            card = None

        meta = Synthesizer._build_order_meta(
            intent_result=intent_result,
            detail=None,
            direct_answer=False,
            card=card,
            contexts=contexts,
            scores=scores,
            tool_block=tool_block,
        )
        yield ("meta", meta)

        prompt = prompt_assembler._build_chat_prompt(
            intent="order_query",
            tool_block=tool_block,
            policy_block="",
            product_block="",
            history_block="",
            query=query,
            context_block=context_block,
        )
        yield from stream_dispatcher.stream_llm(prompt)

        # M14：埋点（DIRECT_ANSWER 走 LLM 综合，card 仅在 N=1 时发 mini）
        # 灰度关闭时 result.total_orders=0 → 计入 zero 分桶（语义：未走 Resolver）
        metrics.inc_resolver_decision(
            action=result.action.value,
            total_orders=result.total_orders,
            card_sent=card is not None,
            card_expected=(result.total_orders == 1 and settings.SSE_CARD_V2),
        )
        # M14 Stage 5：audit 留痕（DIRECT_ANSWER + LLM 路径）
        try_log_action(
            user=None, action="resolver_decision",
            target_type="user", target_id=str(user_id),
            detail={
                "intent": "order_query",
                "resolver_action": result.action.value,
                "resolver_reason": result.reason,
                "total_orders": result.total_orders,
                "card_sent": card is not None,
                "card_density": card.get("density") if card else None,
                "card_type": card.get("type") if card else None,
                "used_llm": True,
            },
        )

    # ---------- M14: meta 构建助手 ----------

    @staticmethod
    def _build_order_meta(
        intent_result: dict,
        detail: Optional[dict],
        direct_answer: bool,
        card: Optional[dict],
        resolver_result: Optional[OrderResolverResult] = None,
        contexts: Optional[list] = None,
        scores: Optional[list] = None,
        tool_block: str = "",
    ) -> dict:
        """构造 order_query meta dict（统一格式，便于 SSE 序列化）。

        Why 独立函数：4 个分支（直答/SHOW_PICKER/NOT_FOUND/DIRECT_ANSWER）都需 meta，
        重复 5+ 次易漂移；集中维护减少 bug。
        """
        meta = {
            "intent": intent_result["intent"],
            "entities": intent_result["entities"],
            "contexts": contexts if contexts is not None else [],
            "scores": scores if scores is not None else [],
            "direct_answer": direct_answer,
        }
        if tool_block:
            meta["tool_result_preview"] = tool_block[:200]
        if resolver_result is not None:
            meta["resolver_action"] = resolver_result.action.value
            meta["resolver_reason"] = resolver_result.reason
            meta["resolver_total_orders"] = resolver_result.total_orders
            if resolver_result.truncated:
                meta["resolver_truncated"] = True
        # SSE Card V2：仅在开启 + 有 card 时塞 meta.card 字段
        if settings.SSE_CARD_V2 and card is not None:
            meta["card"] = card
        return meta

    @staticmethod
    def _build_order_card_payload(
        result: OrderResolverResult, density: str, reason: str,
    ) -> dict:
        """从 Resolver result 构造 OrderCard payload（走 SSE meta.card 字段）。

        Why 不直接 yield Pydantic model：SSE 序列化用 json.dumps，
        dict 更轻量，避免引入 schema-to-dict 转换层。
        """
        items = []
        for o in result.candidate_orders:
            items.append({
                "order_no": o.get("order_no", ""),
                "status": o.get("status", ""),
                "total_amount": float(o.get("total_amount", 0.0)),
                "create_time": o.get("create_time"),
                "item_count": 0,  # 列表场景未展开明细（M10 既有行为）
                "preview": None,
            })
        card = {
            "type": "order_list" if density == "list" else "order_detail",
            "density": density,
            "reason": reason,
            "items": items,
            "truncated": result.truncated,
        }
        if result.effective_order_no and density == "mini":
            card["resolved_order_no"] = result.effective_order_no
        return card

    @staticmethod
    def _handle_product(
        query: str, intent_result: dict, history: Optional[list[dict]],
        sku: Optional[str] = None,
        context_block: str = "",
        search_queries: Optional[list[str]] = None,
    ) -> Generator[Tuple[str, Any], None, None]:
        """product_query：调 ProductTool + 补 policy

        Phase 4 A4：search_queries 不为空时走 PolicyService.search_multi_policy 多路 RRF
        """
        entities = intent_result["entities"]
        # M9.5：优先用 context 传来的 sku（用户从商品详情跳转）
        effective_sku = sku or entities.get("sku")

        # 1. 查商品
        # 优先用 sku 实体精确查；查不到时回退到 keyword 搜（MySQL 里 SKU=SKU001，
        # 但商品 name 包含 ZP1，所以 keyword="ZP1" 能命中 SKU001）
        products = []
        if effective_sku:
            exact = ProductTool.get_by_sku(effective_sku)
            if exact:
                products = [exact]
            else:
                # SKU 实体（如 ZP1）不在 MySQL.sku 列里——keyword 搜名字
                products = ProductTool.search_by_keyword(effective_sku, limit=5)
        if not products:
            # query 整句搜（可能被噪音词干扰）→ 兜底滑动窗口抽 2-3 字实词再搜
            products = ProductTool.search_by_keyword(query, limit=5)
            if not products:
                products = stream_dispatcher.search_by_keyword_window(query, limit=5)

        # 格式化 product 块（P0-LLM 溯源：加 [商品] 标签，让 LLM 知道这是 DB 数据）
        if not products:
            product_block = "[商品] 未在数据库中找到相关商品。"
        else:
            lines = ["[商品] 数据库匹配结果："]
            for p in products:
                attrs = p.get("attributes") or {}
                color = attrs.get("color", [])
                lines.append(
                    f"- SKU {p.get('sku')} | {p.get('name')} | ¥{p.get('price')} | "
                    f"颜色 {color if isinstance(color, str) else '、'.join(color) if color else '—'} | "
                    f"库存 {p.get('stock')}"
                )
            product_block = "\n".join(lines)

        # 2. KB RAG 补 specs（M5 修复 #22 #25：续航/配置 在 KB 不在 MySQL）
        # Phase 4 A4：search_queries 不为空时走多路 RRF；空时走单路（保留旧 mock 兼容）
        if search_queries:
            kb_docs = PolicyService.search_multi_policy(search_queries, top_k=3)
        else:
            kb_docs = PolicyService.search_policy(query, top_k=3)
        kb_block = prompt_assembler._format_policy_docs(kb_docs)
        if kb_block:
            if product_block and "[商品] 未在数据库中找到" not in product_block:
                product_block = f"{product_block}\n\n【商品详细规格（来自知识库 [知识库]）】\n{kb_block}"
            else:
                product_block = f"未在数据库中找到相关商品。\n\n【知识库相关参考】\n{kb_block}"

        # P0-H：暴露商品 + 知识库命中到 meta
        contexts, scores = prompt_assembler._build_meta_contexts(products=products, policy_docs=kb_docs)
        meta = {
            "intent": "product_query",
            "entities": entities,
            "contexts": contexts,
            "scores": scores,
            "products_found": len(products),
            "kb_hits": len(kb_docs),
        }
        yield ("meta", meta)

        # P0-J：商品 + KB 都没命中 → 不调 LLM，直接返兜底文本（防"ZP2 续航怎么样"幻觉）
        if not products and not kb_docs:
            yield from stream_dispatcher.stream_simple(
                f"抱歉，知识库中暂无「{query}」相关资料，无法回答。"
                "如需查询具体商品，请提供准确 SKU 或商品名。"
            )
            return

        prompt = prompt_assembler._build_chat_prompt(
            intent="product_query",
            tool_block="",
            policy_block="",
            product_block=product_block,
            history_block=prompt_assembler._format_history(history),
            query=query,
            context_block=context_block,
        )
        yield from stream_dispatcher.stream_llm(prompt)

    @staticmethod
    def _handle_policy(
        query: str, intent_result: dict, history: Optional[list[dict]],
        context_block: str = "",
        search_queries: Optional[list[str]] = None,
    ) -> Generator[Tuple[str, Any], None, None]:
        """policy_query：纯 PolicyService RAG（最接近 V1.2 行为）

        Phase 4 A4：search_queries 不为空时走多路 RRF；空时单路（保留旧 mock 兼容）
        """
        if search_queries:
            policy_docs = PolicyService.search_multi_policy(search_queries, top_k=5)
        else:
            policy_docs = PolicyService.search_policy(query, top_k=5)
        policy_block = prompt_assembler._format_policy_docs(policy_docs)

        # P0-H：暴露政策命中到 meta
        contexts, scores = prompt_assembler._build_meta_contexts(policy_docs=policy_docs)
        meta = {
            "intent": "policy_query",
            "entities": intent_result["entities"],
            "contexts": contexts,
            "scores": scores,
            "policy_hits": len(policy_docs),
        }
        yield ("meta", meta)

        if not policy_docs:
            # 无相关 policy → LLM 用通用知识兜底（也带 context 让 LLM 知道用户场景）
            ctx_section = f"\n\n【当前场景】\n{context_block}" if context_block else ""
            yield from stream_dispatcher.stream_llm(
                f"参考资料：\n（未检索到相关政策）{ctx_section}\n\n对话历史：\n{prompt_assembler._format_history(history)}\n\n问题：{query}"
            )
            return

        prompt = prompt_assembler._build_chat_prompt(
            intent="policy_query",
            tool_block="",
            policy_block=policy_block,
            product_block="",
            history_block=prompt_assembler._format_history(history),
            query=query,
            context_block=context_block,
        )
        yield from stream_dispatcher.stream_llm(prompt)
