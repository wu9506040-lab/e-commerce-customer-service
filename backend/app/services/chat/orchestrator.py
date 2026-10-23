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
import re
import logging
import re as _re
from typing import Any, Generator, Optional, Tuple

from app.core.config import settings
from app.core import deadline as _deadline
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
from app.core.circuit_breaker import CircuitOpenError
from app.services.query_rewriter import rewrite_query  # M12：指代补全
from app.services.query_rewriter import rewrite_query_multi  # Phase 4 A4：Multi-Query 多路改写
from app.services.session_service import ANONYMOUS_USER_ID
from app.services.chat.prompt_assembler import SYSTEM_PROMPT_BASE  # V16.5 日志取完整 system
from app.tools.product_tool import ProductTool

# P2 长程记忆：用户画像注入（默认 ENABLE_USER_PROFILE=False 灰度）
from app.services import profile_service

from app.services.chat import prompt_assembler, stream_dispatcher
from app.services.chat.refund_handler import handle_refund_v2, handle_refund_v3
from app.services.agents.registry import AgentContext, get_expert  # V21 M3-1
# M14 Stage 5：audit 上报（5 个 resolver action 路径全留痕）
from app.services.audit_service import try_log_action
# M14 Stage 3：BusinessFlow 抽象（仅 refund_query 接入；其他 intent 暂不抽象）
from app.services.business_flow import create_business_flow
# V16.8 P0-A：闲鱼退款 bridge（channel=xianyu 时调，绕过商城 OrderService 登录拦截）
from app.services.xianyu.orchestrator_bridge import handle_xianyu_refund

logger = logging.getLogger(__name__)


# V16.8 P0-C：chitchat 意图短回复（不走 LLM/RAG）
# - 替代 product_query 兜底："在吗/你好" 不再回一长串产品参数
# - 与 config/business_rules/guard.yaml 的 CHITCHAT_RESPONSES.chitchat_template 保持一致（前端如需扩展从 guard.yaml 拉）
CHITCHAT_SHORT_REPLY = "在的~请问您想咨询什么？规格、售后还是价格？"


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
        pre_intent: Optional[dict] = None,
        user_context_block: str = "",
        context_block: str = "",  # V16.3.3：daemon/caller 渠道 context（优先级最高）
        channel: Optional[str] = None,  # V16.8 P0-A：渠道标签（"xianyu" / None=商城）
        buyer_unb: Optional[str] = None,  # V16.8 P0-A：闲鱼买家 unb（refund 路由用）
        system_prompt: Optional[str] = None,  # V18-P0：daemon 全量 system prompt（快速通道用）
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
            pre_intent: chat 入口已分类结果（透传省一次 LLM 调用）
            user_context_block: V15 用户上下文预加载块（最近 5 单 + 画像），chat 入口注入
            channel: V16.8 P0-A 渠道标签；"xianyu" 时 refund_query 路由到闲鱼版
            buyer_unb: V16.8 P0-A 闲鱼买家 unb，refund 路由用

        Yields:
            ("meta", {intent, entities, ...})
            ("token", str)
            ("done", {"answer": str})
        """
        if not query or not query.strip():
            raise ValueError("query 不能为空")
        query = query.strip()

        # V18：RAG 渠道隔离——channel=xianyu 时检索只命中闲鱼 KB（payload.channel
        # 过滤，contextvar 每请求独立），商城 KB 不串台。快速通道不走 RAG，
        # 这里管的是 fallback 降级路径的隔离。
        if channel == "xianyu":
            try:
                from app.services.policy_service import set_kb_channel
                set_kb_channel("xianyu")
            except Exception:
                pass

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

        # V18-P0（2026-10-17）：xianyu IM 快速通道 —— 实测 rewrite(LLM) +
        # multi-query(LLM) + RAG 检索前置吃掉 ~20s，是 IM 回复慢的主因。
        # daemon 侧已完成本地意图路由/场景 yaml/商品信息/画像注入（system_prompt
        # 全量传入），这里直接：session 历史 + system_prompt + query → LLM 流式。
        # 例外：chitchat 走免 LLM 短路；refund_query/order_query 落到下方
        # 专用分支（要查 DB），不快速通道。
        if channel == "xianyu" and system_prompt:
            _primary = (pre_intent or {}).get("primary", "product_query")
            if _primary == "chitchat":
                logger.info(
                    f"synth.xianyu_fast: chitchat query={query[:30]}",
                    extra={"intent": "chitchat", "channel": "xianyu"},
                )
                yield ("meta", {
                    "intent": "chitchat", "entities": {}, "contexts": [],
                    "scores": [], "channel": channel,
                    "chitchat_short_circuit": True,
                })
                yield ("token", CHITCHAT_SHORT_REPLY)
                yield ("done", {"answer": CHITCHAT_SHORT_REPLY,
                                "chitchat_short_circuit": True})
                return
            if _primary not in ("refund_query", "order_query"):
                logger.info(
                    f"synth.xianyu_fast: direct LLM query={query[:40]} user_id={user_id}",
                    extra={"intent": "xianyu_fast", "channel": "xianyu"},
                )
                from app.services.chat.stream_dispatcher import stream_llm as _sllm
                yield from _sllm(
                    query, system_prompt=system_prompt, history=history or []
                )
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

        # V16.3.3：caller 已传 context_block → 直接用，跳过 M9.5/V15 内部拼装
        # （避免 chat.py 透传的渠道 context 被内部 _build_context_block 覆盖。
        #  缺省 None 时走 M9.5/V15 内部拼装——前端 / 老 daemon 仍按原路径工作）
        if not context_block:
            context_block = prompt_assembler._build_context_block(
                sku, order_no, user_id, profile_block=profile_block,
                user_context_block=user_context_block,  # V15：会话入口预加载
            )
        if context_block:
            logger.info(
                f"synth.context: sku={sku} order_no={order_no} "
                f"context_len={len(context_block)} profile_len={len(profile_block)} "
                f"user_id={user_id}",
                extra={"intent": "context"},
            )

        # 1. 意图分类（V12 多意图：primary 走 4 类分派；secondary 注入 prompt）
        # V13（2.2）：消除双分类——chat.py 入口为缓存决策/V10-A 已 classify(raw query)
        # 一次，规则 miss 时此前同一请求会再调一次 LLM 分类（且输入版本可能不同）。
        # 复用条件严格：pre_intent 存在 且 本轮改写未改变 query——改写过的必须重分。
        if pre_intent is not None and not was_rewritten:
            intent_result = pre_intent
            logger.debug("synth.intent: 复用 chat 预分类（省一次 LLM 调用）")
            # V16.5 全链路日志：Intent 复用（来自 pre_intent）
            try:
                from app.core.runtime_log import log_event as _relog
                _relog(
                    "INTENT_CLASSIFY", "INFO", str(session_id or "-"), str(user_id or "-"),
                    f"primary={intent_result.get('primary','?')} (reused pre_intent)",
                    method=pre_intent.get("method", "?"),
                    primary=pre_intent.get("primary", "?"),
                    confidence=round(pre_intent.get("confidence", 0), 3),
                )
            except Exception:
                pass
        else:
            intent_result = IntentService.classify(query)
            # V16.5 全链路日志：Intent 分类结果
            try:
                from app.core.runtime_log import log_event as _relog
                _relog(
                    "INTENT_CLASSIFY", "INFO", str(session_id or "-"), str(user_id or "-"),
                    f"primary={intent_result.get('primary','?')} confidence={intent_result.get('confidence', 0):.2f}",
                    method=intent_result.get("method", "?"),
                    primary=intent_result.get("primary", "?"),
                    confidence=round(intent_result.get("confidence", 0), 3),
                    intents=intent_result.get("intents", []),
                    entities=intent_result.get("entities", {}),
                )
            except Exception:
                pass

        # V13（2.3）：内部事件携带当轮意图决策快照——chat.py 消费后随消息落库
        # （intent_snapshot 列），不外发 SSE。👎/badcase 反查的 join 地基：
        # 没有它，差评只能 join 到"检索了什么"，答不出"当时判成什么意图、
        # 走规则还是 LLM、是否触发过升级"。
        yield ("_intent", {
            "primary": intent_result.get("primary"),
            "method": intent_result.get("method"),
            "confidence": intent_result.get("confidence"),
            "intents": intent_result.get("intents", []),
            "upgraded": bool(intent_result.get("upgraded")),
        })
        primary = intent_result["primary"]  # V12：主意图（替代 V11 的 intent_result["intent"]）
        entities = intent_result["entities"]
        # V12：构造 secondary_intent_block（让 LLM 知道用户可能还想问 X 类问题）
        # 取 intents 中除 primary 外的全部作为 secondary（intents 已按 confidence 降序）
        all_intents = intent_result.get("intents", [])
        secondary_intents = [i for i in all_intents if i["intent"] != primary]
        secondary_intent_block = ""
        if secondary_intents:
            secondary_lines = [
                f"- {i['intent']}（置信度 {i['confidence']:.2f}）"
                for i in secondary_intents
            ]
            secondary_intent_block = (
                "用户问题可能还涉及以下意图，请在回答 primary 意图时一并简要覆盖：\n"
                + "\n".join(secondary_lines)
            )
        # M8：intent 用 extra 显式带（避免 ContextVar 跨 thread context 不可 reset 的问题）
        logger.info(
            f"synth.start: primary={primary} secondary={[i['intent'] for i in secondary_intents]} "
            f"method={intent_result['method']} conf={intent_result['confidence']:.2f} user_id={user_id}",
            extra={"intent": primary},
        )

        # 2. 分派（按 primary intent 调用对应 service/tool · V12 多意图）
        # V13（1.3 修复，2026-10-10）：构建分派生成器与"消费+异常兜底"分离——
        # 修复前：token 已推给客户端后 handler 抛异常 → 从头再跑 V1.2 整流 fallback，
        # chat.py 的 full_answer=半截+重答全文 拼接落库（双 meta/双份答案）。
        # 现在：已发过 token 就不再重答，只追加固定收尾话术+done；未发过才允许整流 fallback。
        #
        # V16.8 P0-C：chitchat 短路 — "在吗/你好" 类不再调 LLM，直接 1 个短 token + done
        # - 替代 product_query 的长篇 RAG 兜底，省 token 提升响应
        # - meta.intent="chitchat" 字面返回（前端可识别）
        # - 已发过 token 才进入的兜底分支与 chitchat 短路互不干扰（chitchat 全程未调 LLM）
        if primary == "chitchat":
            logger.info(
                f"synth.chitchat_short_circuit: query={query[:40]} user_id={user_id}",
                extra={"intent": "chitchat", "channel": channel},
            )
            metrics.inc_chat("chitchat", v3_engine="-")  # M8：chitchat 路径独立计数
            yield ("meta", {
                "intent": "chitchat",
                "entities": intent_result.get("entities", {}),
                "contexts": [],
                "scores": [],
                "channel": channel,
                "chitchat_short_circuit": True,
            })
            yield ("token", CHITCHAT_SHORT_REPLY)
            yield ("done", {
                "answer": CHITCHAT_SHORT_REPLY,
                "chitchat_short_circuit": True,
            })
            return
        try:
            # V21 M3-1：多 Agent 协作制——意图 → 意图域专家实体（AgentProtocol）
            # 分派决策收敛到专家（RefundAgent 内含 xianyu/Flow/V3/V2 四路），
            # orchestrator 只负责组装上下文与事件流透传；行为与原 if/elif 等价。
            _expert = get_expert(primary)
            if _expert is None:
                # 兜底：未注册意图按 policy 专家处理（保持旧行为）
                logger.warning(f"intent {primary!r} 无注册专家，回落 policy_expert", extra={"intent": primary})
                _expert = get_expert("policy_query")
            _ctx = AgentContext(
                query=query, user_id=user_id, intent_result=intent_result,
                session_id=session_id, order_no=order_no, sku=sku,
                context_block=context_block, history=history,
                secondary_intent_block=secondary_intent_block,
                search_queries=search_queries, channel=channel, buyer_unb=buyer_unb or "",
            )
            _expert.refine(_ctx)  # 转交钩子 v1：专家可重定向意图（redirect_intent）
            if _ctx.redirect_intent and _ctx.redirect_intent != primary:
                logger.info(
                    f"agent handoff: {primary} → {_ctx.redirect_intent}",
                    extra={"intent": primary, "handoff": _ctx.redirect_intent},
                )
                metrics.inc_agent_handoff(primary, _ctx.redirect_intent)
                _redirected = get_expert(_ctx.redirect_intent)
                if _redirected is not None:
                    primary = _ctx.redirect_intent
                    _expert = _redirected
            dispatch_gen = _expert.dispatch(_ctx)
            # V21 M3-2：双意图双专家协作——主专家答完补答次意图（灰度开关）
            if getattr(settings, "ENABLE_DUAL_INTENT_DISPATCH", False):
                from app.services.agents.coordinator import chain_with_secondary

                dispatch_gen = chain_with_secondary(dispatch_gen, _ctx, primary)

            yielded_token = False
            try:
                for event in dispatch_gen:
                    if event[0] == "token":
                        yielded_token = True
                    yield event
            except (CircuitOpenError, _deadline.DeadlineExceededError) as e:
                # V14（3.3）：spec 点名——断路器 OPEN 或预算耗尽时，V1.2 整流
                # fallback 也会二次撞墙（同一把 LLM），直接固定话术收尾，
                # 让用户拿到一个简短反馈而不是再等一次最终失败（兜底也耗预算）
                metrics.record_fallback_fastfail()
                logger.warning(
                    f"synth.dispatch fast-fail（不再二次调 LLM）: primary={primary}, "
                    f"yielded_token={yielded_token}, err={type(e).__name__}",
                    extra={"intent": primary},
                )
                if yielded_token:
                    yield ("token", "\n（抱歉，刚才的回复中断了。您可以重试，或说「转人工」由人工客服协助。）")
                else:
                    yield ("token", "\n（系统繁忙，请稍后重试，或说「转人工」由人工客服协助。）")
                yield ("done", {"answer": "", "fast_fail": True})
                return
            except Exception as e:
                logger.exception(
                    f"synth.dispatch 异常: primary={primary}, yielded_token={yielded_token}, err={e}",
                    extra={"intent": primary},
                )
                if yielded_token:
                    # V13（1.3）：半截已送达 → 禁止整流重答（防拼接污染），固定话术收尾
                    yield ("token", "\n（抱歉，刚才的回复中断了。您可以重试，或说「转人工」由人工客服协助。）")
                    yield ("done", {"answer": ""})
                else:
                    # 未发过 token → 安全地走 V1.2 整流 fallback
                    # 注意：fallback 不带 user_id（V1.2 pipeline 不接收 user_id）
                    for event_type, data in v12_rag_run_stream(query, 5, history):
                        yield (event_type, data)
        except (CircuitOpenError, _deadline.DeadlineExceededError) as e:
            # 外层保险：构建阶段就 OPEN/超预算——同 fast-fail
            metrics.record_fallback_fastfail()
            logger.warning(
                f"synth.dispatch 构建期 fast-fail: primary={primary}, err={type(e).__name__}",
                extra={"intent": primary},
            )
            yield ("token", "\n（系统繁忙，请稍后重试，或说「转人工」由人工客服协助。）")
            yield ("done", {"answer": "", "fast_fail": True})
            return
        except Exception as e:
            # 外层保险：分派生成器构建阶段的异常（极少见）
            logger.exception(
                f"synth.dispatch 构建异常，fallback 到 V1.2 RAG: primary={primary}, err={e}",
                extra={"intent": primary},
            )
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
        secondary_intent_block: str = "",  # V12：多意图 secondary 提示
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
            secondary_intent_block=secondary_intent_block,  # V12
        )
        yield from stream_dispatcher.stream_llm(prompt, context_block=context_block)

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

    # V21：列表型问题 → 全量商品清单（此前整句 keyword 搜不到 → LLM 答"没有详细信息"）
    _PRODUCT_LIST_PAT = re.compile(
        r"(哪些产品|有哪些产品|有什么产品|卖什么|产品列表|商品列表|都卖|在售商品|有哪些商品)"
    )

    @classmethod
    def _maybe_list_products(cls, query: str) -> Optional[list]:
        """列表型问题返回全量在售商品；非列表型返回 None（走原 keyword 搜）。"""
        if not cls._PRODUCT_LIST_PAT.search(query or ""):
            return None
        return ProductTool.list_products(limit=20)  # 全量在售（DB 当前 10 款，上限防膨胀）

    @staticmethod
    def _handle_product(
        query: str, intent_result: dict, history: Optional[list[dict]],
        sku: Optional[str] = None,
        context_block: str = "",
        search_queries: Optional[list[str]] = None,
        secondary_intent_block: str = "",  # V12：多意图 secondary 提示
    ) -> Generator[Tuple[str, Any], None, None]:
        """product_query：调 ProductTool + 补 policy

        Phase 4 A4：search_queries 不为空时走 PolicyService.search_multi_policy 多路 RRF
        """
        entities = intent_result["entities"]
        # M9.5：优先用 context 传来的 sku（用户从商品详情跳转）
        effective_sku = sku or entities.get("sku")

        # V16.5 全链路日志：SENTINEL — _handle_product 入口确认
        try:
            from app.core.runtime_log import log_event as _relog
            _relog(
                "_HANDLE_PRODUCT_ENTRY", "INFO", str(locals().get('session_id', '-')), str(locals().get('user_id', '-')),
                f"_handle_product ENTER query={query[:80]} sku={effective_sku}",
                query=query[:200],
                sku=effective_sku,
            )
        except Exception:
            pass

        # 1. 查商品
        # 优先用 sku 实体精确查；查不到时回退到 keyword 搜（MySQL 里 SKU=SKU001，
        # 但商品 name 包含 ZP1，所以 keyword="ZP1" 能命中 SKU001）
        products = []
        # V21 产品规则（店铺级 vs 商品级）：
        # - 从商品详情页进入（effective_sku 存在）= 用户想咨询**这个品** → 单品优先，不做全量清单
        # - 店铺入口泛问"你们有哪些产品"（无 sku）→ 返回全量在售清单（keyword 搜不出商品名）
        if not effective_sku:
            listed = Synthesizer._maybe_list_products(query)
            if listed is not None:
                products = listed
        if not products and effective_sku:
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
        # V16.5 全链路日志：L4 RAG 检索 Top-3 + scores
        try:
            from app.core.runtime_log import log_event as _relog
            # Top-3（products + kb_docs 合并，按 scores 排序）
            merged = []
            for c, s in zip(contexts, scores):
                if isinstance(c, dict):
                    src = c.get("source", c.get("title", ""))
                    title = c.get("title", "")
                    preview = (c.get("text_preview", "") or c.get("text", ""))[:200]
                    merged.append((src, title, s, preview))
            merged.sort(key=lambda x: x[2] if isinstance(x[2], (int, float)) else 0, reverse=True)
            top3 = merged[:3]
            top3_lines = [
                f"[{i+1}] source={src} score={s:.4f} title={title[:50]} preview={preview[:80]}"
                for i, (src, title, s, preview) in enumerate(top3)
            ]
            _relog(
                "RAG_RETRIEVE", "INFO", str(locals().get('session_id', '-')), str(locals().get('user_id', '-')),
                f"Top-{len(top3)} retrieved (total={len(contexts)}): " + " | ".join(top3_lines),
                hits=len(contexts),
                top1_source=top3[0][0] if top3 else "-",
                top1_score=top3[0][2] if top3 and isinstance(top3[0][2], (int, float)) else 0,
                top2_source=top3[1][0] if len(top3) > 1 else "-",
                top2_score=top3[1][2] if len(top3) > 1 and isinstance(top3[1][2], (int, float)) else 0,
                top3_source=top3[2][0] if len(top3) > 2 else "-",
                top3_score=top3[2][2] if len(top3) > 2 and isinstance(top3[2][2], (int, float)) else 0,
                query=query[:200],
            )
        except Exception as _e:
            # 诊断：让异常可见（CI 看到 stderr）
            import sys as _sys
            print(f"[RAG_RETRIEVE_LOG_FAIL] {_e}", file=_sys.stderr)
        meta = {
            "intent": "product_query",
            "entities": entities,
            "contexts": contexts,
            "scores": scores,
            "products_found": len(products),
            "kb_hits": len(kb_docs),
        }
        yield ("meta", meta)

        # V16.3.4：商品 + KB 双空时分两路——
        #   - caller 传了 context_block（渠道上下文里有商品话术/买家画像）
        #     → 走 stream_llm 兜底：trust V16.3.4 promote-to-system 的事实，LLM 真可答
        #   - 没 context_block → 保留 P0-J 防幻觉 hardcode 模板（前端通用路径行为不变）
        if not products and not kb_docs:
            # V16.5 全链路日志：L1 P0-J 早返点（RAG 双空 fallback）
            try:
                from app.core.runtime_log import log_event as _relog
                _relog(
                    "RAG_EMPTY_FALLBACK", "INFO", str(locals().get('session_id', '-')), str(locals().get('user_id', '-')),
                    f"P0-J 兜底: products=0 kb_docs=0 context_block_len={len(context_block)}",
                    fallback="stream_llm" if context_block else "stream_simple_hardcode",
                    products=len(products),
                    kb_docs=len(kb_docs),
                    context_block_len=len(context_block),
                    query=query[:200],
                )
            except Exception:
                pass
            if context_block:
                yield from stream_dispatcher.stream_llm(
                    f"参考资料：（未检索到相关商品 / 知识库；卖家渠道上下文见 system 段）\n\n"
                    f"对话历史：{prompt_assembler._format_history(history)}\n\n"
                    f"问题：{query}",
                    context_block=context_block,
                )
            else:
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
            secondary_intent_block=secondary_intent_block,  # V12
        )
        # V16.5 全链路日志：LLM_PROMPT（system prompt 前 3000 字 + user 完整）
        try:
            from app.core.runtime_log import log_event as _relog
            # V16.5 修：取完整 system 长度（SYSTEM_PROMPT_BASE + context_block,不只是 context_block）
            _full_system = (SYSTEM_PROMPT_BASE + context_block) if context_block else SYSTEM_PROMPT_BASE
            system_prompt = _full_system[:3000] + ("...(截断)" if len(_full_system) > 3000 else "")
            _relog(
                "LLM_PROMPT", "INFO", str(locals().get('session_id', '-')), str(locals().get('user_id', '-')),
                f"system_len={len(_full_system)} user_len={len(query)}",
                system_prompt=system_prompt,
                user_message=query,  # user 不截断
                chars_system=len(context_block),
                chars_user=len(query),
            )
        except Exception:
            pass
        yield from stream_dispatcher.stream_llm(
            prompt, context_block=context_block,
            max_tokens=1400 if listed is not None else 256,  # V21：清单需要更长输出
        )

    @staticmethod
    def _handle_policy(
        query: str, intent_result: dict, history: Optional[list[dict]],
        context_block: str = "",
        search_queries: Optional[list[str]] = None,
        secondary_intent_block: str = "",  # V12：多意图 secondary 提示
    ) -> Generator[Tuple[str, Any], None, None]:
        """policy_query：纯 PolicyService RAG（最接近 V1.2 行为）

        Phase 4 A4：search_queries 不为空时走多路 RRF；空时单路（保留旧 mock 兼容）
        """
        # V16.5 全链路日志：HANDLE_POLICY 入口
        try:
            from app.core.runtime_log import log_event as _relog
            _sid = locals().get('session_id', '-')
            _uid = locals().get('user_id', '-')
            _relog(
                "HANDLE_POLICY", "INFO", str(_sid), str(_uid),
                f"ENTER query={query[:80]} search_queries={search_queries}",
                query=query[:200], search_queries=search_queries,
            )
        except Exception:
            pass
        if search_queries:
            policy_docs = PolicyService.search_multi_policy(search_queries, top_k=5)
        else:
            policy_docs = PolicyService.search_policy(query, top_k=5)
        # V16.5 全链路日志：RAG_RETRIEVE（policy 检索 Top）
        try:
            from app.core.runtime_log import log_event as _relog
            _sid = locals().get('session_id', '-')
            _uid = locals().get('user_id', '-')
            top3 = policy_docs[:5] if policy_docs else []
            top3_lines = [
                f"[{i+1}] title={(d.get('title','') if isinstance(d,dict) else '')[:50]}"
                for i, d in enumerate(top3)
            ]
            _relog(
                "RAG_RETRIEVE", "INFO", str(_sid), str(_uid),
                f"policy Top-{len(top3)} retrieved (total={len(policy_docs)}): " + " | ".join(top3_lines),
                hits=len(policy_docs), query=query[:200],
            )
        except Exception:
            pass
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
            # 无相关 policy → LLM 用通用知识兜底（context_block 走 stream_llm 注入 system 不在此重复）
            try:
                from app.core.runtime_log import log_event as _relog
                _sid = locals().get('session_id', '-')
                _uid = locals().get('user_id', '-')
                _full_system = (SYSTEM_PROMPT_BASE + context_block) if context_block else SYSTEM_PROMPT_BASE
                _relog(
                    "LLM_PROMPT", "INFO", str(_sid), str(_uid),
                    f"policy fallback system_len={len(_full_system)} user_len={len(query)}",
                    system_prompt=_full_system[:3000] + ("...(截断)" if len(_full_system) > 3000 else ""),
                    user_message=query, chars_system=len(_full_system),
                )
            except Exception:
                pass
            yield from stream_dispatcher.stream_llm(
                f"参考资料：\n（未检索到相关政策）\n\n对话历史：\n{prompt_assembler._format_history(history)}\n\n问题：{query}",
                context_block=context_block,
            )
            return

        prompt = prompt_assembler._build_chat_prompt(
            intent="policy_query",
            tool_block="",
            policy_block=policy_block,
            product_block="",
            history_block=prompt_assembler._format_history(history),
            query=query,
            # V16.3.4：context_block 已在下方 stream_llm promote 到 system message（架构修）
            secondary_intent_block=secondary_intent_block,  # V12
        )
        # V16.5 全链路日志：policy_query LLM_PROMPT（完整 system 长度）
        try:
            from app.core.runtime_log import log_event as _relog
            _sid = locals().get('session_id', '-')
            _uid = locals().get('user_id', '-')
            _full_system = (SYSTEM_PROMPT_BASE + context_block) if context_block else SYSTEM_PROMPT_BASE
            _relog(
                "LLM_PROMPT", "INFO", str(_sid), str(_uid),
                f"policy_query system_len={len(_full_system)} user_len={len(query)}",
                system_prompt=_full_system[:3000] + ("...(截断)" if len(_full_system) > 3000 else ""),
                user_message=query, chars_system=len(_full_system),
            )
        except Exception:
            pass
        yield from stream_dispatcher.stream_llm(prompt, context_block=context_block)
