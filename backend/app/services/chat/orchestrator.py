"""
Orchestrator（Sprint 3 拆分自 synthesizer.py）

职责：intent 分派 + 业务编排（chat handler 是"万能模块"转"调度器"）。
- run_stream 主入口
- _try_direct_answer_order 直答兜底
- _handle_order / _handle_refund_v2 / _handle_refund_v3 / _handle_product / _handle_policy
- _DIRECT_ANSWER_PATTERNS 直答关键词
- 分派异常 → fallback 到 V1.2 统一 RAG

边界：不构造 prompt（委托 prompt_assembler）；不做 LLM 流式（委托 stream_dispatcher）。

范围引用：
- 系统 Prompt：经由 prompt_assembler.SYSTEM_PROMPT_BASE（最终走 prompt_loader）
- LLM 调用：经由 stream_dispatcher.stream_llm
- 简单文本：经由 stream_dispatcher.stream_simple
- 滑动窗口：经由 stream_dispatcher.search_by_keyword_window
- intent 分派：本模块完成
"""
import logging
import re as _re
from typing import Any, Generator, Optional, Tuple

from app.core.config import settings
from app.services.intent_service import IntentService
from app.services.metrics import metrics
from app.services.order_service import OrderService
from app.services.policy_service import PolicyService
from app.services.refund_graph import refund_graph_app  # V3 LangGraph 版
from app.services.refund_service import RefundService
from app.services.rag.pipeline import run_stream as v12_rag_run_stream
from app.services.query_rewriter import rewrite_query  # M12：指代补全
from app.services.session_service import ANONYMOUS_USER_ID
from app.tools.product_tool import ProductTool

from app.services.chat import prompt_assembler, stream_dispatcher

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
    ) -> Generator[Tuple[str, Any], None, None]:
        """
        主入口：分类 → 分派 → 融合 → LLM 流式输出

        Args:
            query: 用户问题
            user_id: 用户 ID（未登录为 ANONYMOUS_USER_ID = 0）
            history: 多轮历史 [{"role":..., "content":...}]
            sku: 当前商品 SKU（M9.5：从 /shop/:sku 跳转携带，注入 prompt 让 LLM 知道是哪款）
            order_no: 当前订单号（M9.5：从 OrderCard 跳转携带，注入 prompt 让 LLM 知道是哪个订单）

        Yields:
            ("meta", {intent, entities, ...})
            ("token", str)
            ("done", {"answer": str})
        """
        if not query or not query.strip():
            raise ValueError("query 不能为空")
        query = query.strip()

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

        # M9.5：预加载 context（商品/订单详情），后续注入 LLM prompt
        context_block = prompt_assembler._build_context_block(sku, order_no, user_id)
        if context_block:
            logger.info(
                f"synth.context: sku={sku} order_no={order_no} "
                f"context_len={len(context_block)} user_id={user_id}",
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
                yield from Synthesizer._handle_order(query, user_id, intent_result, order_no=order_no, context_block=context_block)
                return
            elif intent == "refund_query":
                # V3 开关：USE_LANGGRAPH_REFUND=true 时走 LangGraph 版
                if settings.USE_LANGGRAPH_REFUND:
                    logger.info("refund_query → LangGraph V3", extra={"intent": intent})
                    metrics.inc_chat(intent, v3_engine="v3")  # M8
                    yield from Synthesizer._handle_refund_v3(query, user_id, intent_result, order_no=order_no, context_block=context_block, history=history)
                else:
                    metrics.inc_chat(intent, v3_engine="v2")  # M8
                    yield from Synthesizer._handle_refund_v2(query, user_id, intent_result, order_no=order_no, context_block=context_block)
                return
            elif intent == "product_query":
                metrics.inc_chat(intent, v3_engine="-")  # M8
                yield from Synthesizer._handle_product(query, intent_result, history, sku=sku, context_block=context_block)
                return
            else:  # policy_query
                metrics.inc_chat(intent, v3_engine="-")  # M8
                yield from Synthesizer._handle_policy(query, intent_result, history, context_block=context_block)
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
    ) -> Generator[Tuple[str, Any], None, None]:
        """order_query：调 OrderService"""
        entities = intent_result["entities"]
        # M9.5：优先用 context 传来的 order_no（用户从订单卡片跳转），其次 intent 抽取
        effective_order_no = order_no or entities.get("order_no")

        if user_id == ANONYMOUS_USER_ID:
            # 未登录 → 不报错，返回"请登录"
            yield ("meta", {
                "intent": "order_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
            })
            yield from stream_dispatcher.stream_simple(prompt_assembler.NO_LOGIN_PROMPT)
            return

        # M11.5：先试工具直答（"什么状态"类简单查询，不调 LLM）
        direct = Synthesizer._try_direct_answer_order(
            query, user_id, entities, order_no
        )
        if direct is not None:
            yield ("meta", {
                "intent": "order_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
                "direct_answer": True,
            })
            yield from stream_dispatcher.stream_simple(direct)
            return

        if effective_order_no:
            detail = OrderService.get_order_detail(user_id, effective_order_no)
            if not detail:
                tool_block = f"订单 {effective_order_no} 不存在或不属于当前用户。"
                contexts, scores = [], []
            else:
                tool_block = prompt_assembler._format_tool_result("order_query", detail)
                contexts, scores = prompt_assembler._build_meta_contexts(tool_result=detail)
        else:
            # 无 order_no → 列最近订单（OrderService.list_user_orders 不支持 limit，按默认上限返回）
            orders = OrderService.list_user_orders(user_id)
            tool_block = prompt_assembler._format_tool_result("order_query", {"orders": orders})
            contexts, scores = prompt_assembler._build_meta_contexts(tool_result={"orders": orders})

        meta = {
            "intent": "order_query",
            "entities": entities,
            "contexts": contexts,
            "scores": scores,
            "tool_result_preview": tool_block[:200] if tool_block else "",
        }
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

    @staticmethod
    def _handle_refund_v2(
        query: str, user_id: int, intent_result: dict,
        order_no: Optional[str] = None,
        context_block: str = "",
    ) -> Generator[Tuple[str, Any], None, None]:
        """refund_query V2.x：调 RefundService（复合 tool + policy）

        V3 起作为 fallback：USE_LANGGRAPH_REFUND=false 时使用，或 LangGraph 版异常时回退。

        .. deprecated::
            V3 LangGraph refund_graph 上线后的临时双轨态。V3 稳定后（预计下一个里程碑）
            删除本函数 + 关闭 USE_LANGGRAPH_REFUND 开关 + 删除对应测试。
            截止 2026-06-28：chat_e2e #5/#6 已用 V3 路径通过。
        """
        entities = intent_result["entities"]
        # M9.5：优先用 context 传来的 order_no（用户从订单卡片跳转退款）
        effective_order_no = order_no or entities.get("order_no")

        if user_id == ANONYMOUS_USER_ID:
            yield ("meta", {
                "intent": "refund_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
            })
            yield from stream_dispatcher.stream_simple(prompt_assembler.NO_LOGIN_PROMPT)
            return

        # 无 order_no：取最近一笔订单的 order_no
        if not effective_order_no:
            recent = OrderService.list_user_orders(user_id)
            recent = recent[:1] if recent else []
            if recent:
                effective_order_no = recent[0]["order_no"]
            else:
                yield ("meta", {
                    "intent": "refund_query",
                    "entities": entities,
                    "contexts": [],
                    "scores": [],
                })
                yield from stream_dispatcher.stream_simple("用户当前没有订单，无法判断退款。请提供订单号。")
                return

        result = RefundService.check_refundable_with_policy(user_id, effective_order_no, query)
        tool_block = prompt_assembler._format_tool_result("refund_query", result)
        policy_docs = result.get("policy_docs", [])
        policy_block = prompt_assembler._format_policy_docs(policy_docs)

        # P0-H：把退款判断 + 政策命中一并暴露给 meta
        contexts, scores = prompt_assembler._build_meta_contexts(policy_docs=policy_docs)
        meta = {
            "intent": "refund_query",
            "entities": entities,
            "contexts": contexts,
            "scores": scores,
            "order_no": effective_order_no,
            "refundable": result.get("tool_result", {}).get("refundable"),
            "policy_hits": len(policy_docs),
        }
        yield ("meta", meta)

        prompt = prompt_assembler._build_chat_prompt(
            intent="refund_query",
            tool_block=tool_block,
            policy_block=policy_block,
            product_block="",
            history_block="",
            query=query,
            context_block=context_block,
        )
        yield from stream_dispatcher.stream_llm(prompt)

    @staticmethod
    def _handle_refund_v3(
        query: str, user_id: int, intent_result: dict,
        order_no: Optional[str] = None,
        context_block: str = "",
        history: Optional[list[dict]] = None,
    ) -> Generator[Tuple[str, Any], None, None]:
        """refund_query V3：走 LangGraph refund_graph_app.stream()

        与 V2 区别：
        - LLM 调用在 LangGraph Node 6（synthesize_answer），不在 synthesizer
        - 支持「质量问题无凭证 → escalate」升级人工路径
        - LangGraph 异常 → fallback 到 _handle_refund_v2

        SSE 协议兼容：
        - judge Node → yield meta（含 refundable / reason / days_since_order）
        - fetch_policy Node → 仅 log，不 yield meta
        - synthesize / escalate Node → yield token（final_answer 作为整体 token）
        - done 事件由 api/chat.py 统一处理（write-through）

        M9.5：context_block 透传给 LangGraph state，让 synthesize_answer 节点能看到订单 context
        M9.5+：history 透传给 LangGraph state，让 synthesize_answer / judge 能从历史提取 order_no
        """
        entities = intent_result["entities"]
        # M9.5：优先用 context 传来的 order_no（用户从订单卡片跳转退款）
        # M9.5+：其次用 intent 解析出的；最后从 history 中最近一条提到 ORD... 的消息兜底
        effective_order_no = (
            order_no
            or entities.get("order_no")
            or prompt_assembler._extract_order_no_from_history(history)
        )

        # 1. 鉴权（与 V2 一致）
        if user_id == ANONYMOUS_USER_ID:
            yield ("meta", {
                "intent": "refund_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
                "v3_engine": "langgraph",
            })
            yield from stream_dispatcher.stream_simple(prompt_assembler.NO_LOGIN_PROMPT)
            return

        # 2. order_no 兜底（M9.5 修复：禁止自动 fallback 到最近订单，防止串单）
        # 根因：之前 fallback 会偷换成「最近订单」，导致 LLM 用错误订单的事实回答
        # 修复：无 order_no 时直接请用户提供，禁止推测
        if not effective_order_no:
            yield ("meta", {
                "intent": "refund_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
                "v3_engine": "langgraph",
            })
            yield from stream_dispatcher.stream_simple(
                "请提供要查询退款的订单号（格式示例：ORD20260628004）。"
            )
            return

        # 3. 调 LangGraph refund_graph_app.stream() 边执行边输出
        meta_emitted = False
        try:
            for event in refund_graph_app.stream(
                {
                    "user_id": user_id,
                    "order_no": effective_order_no,
                    "query": query,
                    "context_block": context_block,  # M9.5：注入 context 让 synthesize 看得到
                    "history": history or [],  # M9.5+：注入历史让 synthesize 能引用上下文
                },
                stream_mode="updates",  # 每步返回 {node_name: state_update}
            ):
                for node_name, state_update in event.items():
                    # 跳过 __start__ / __end__ 哨兵节点
                    if node_name.startswith("__"):
                        continue

                    if node_name == "judge":
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "v3_engine": "langgraph",
                            "refundable": state_update.get("refundable"),
                            "reason": state_update.get("reason"),
                            "days_since_order": state_update.get("days_since_order"),
                        })
                        meta_emitted = True
                    elif node_name == "fetch_policy":
                        logger.info(
                            f"refund_v3 fetch_policy: order={effective_order_no} "
                            f"hits={len(state_update.get('policy_docs', []))}"
                        )
                    elif node_name in ("synthesize", "escalate"):
                        if not meta_emitted:
                            # 兜底：理论上 judge 一定先于 synthesize
                            yield ("meta", {
                                "intent": "refund_query",
                                "entities": entities,
                                "contexts": [],
                                "scores": [],
                                "order_no": effective_order_no,
                                "v3_engine": "langgraph",
                            })
                            meta_emitted = True
                        chunk = state_update.get("final_answer", "")
                        if chunk:
                            yield ("token", chunk)
            # 修复：refund_v3 主 LangGraph 流完成后补 yield done
            # 根因：_stream_llm/_stream_simple 末尾自动 yield done，但 LangGraph 路径不走它们
            # 影响：chat.py StopIteration → break → 缺 SSE done + write-through + latency 埋点
            yield ("done", {"answer": ""})
        except Exception as e:
            # LangGraph 挂了 → fallback 到 V2（保险丝）
            logger.exception(
                f"LangGraph refund 图执行失败，fallback 到 V2: order={effective_order_no} err={e}"
            )
            yield from Synthesizer._handle_refund_v2(query, user_id, intent_result, order_no=order_no, context_block=context_block)

    @staticmethod
    def _handle_product(
        query: str, intent_result: dict, history: Optional[list[dict]],
        sku: Optional[str] = None,
        context_block: str = "",
    ) -> Generator[Tuple[str, Any], None, None]:
        """product_query：调 ProductTool + 补 policy"""
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
    ) -> Generator[Tuple[str, Any], None, None]:
        """policy_query：纯 PolicyService RAG（最接近 V1.2 行为）"""
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
