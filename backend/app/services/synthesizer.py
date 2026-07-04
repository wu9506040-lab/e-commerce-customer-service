"""
Response Synthesizer - 多源融合层（M4 新增）

按 PROJECT_DESIGN.md §3 + §7：
- Intent Classifier 决定走哪条路径
- 不同路径调不同 service / tool
- 多源结果（tool + RAG + history）融合成一个 prompt 喂给单一 LLM
- 流式输出

设计约束（§7）：
- 优先级：Tool 结构化数据 > 用户上下文 > Policy RAG > Product RAG > 对话历史
- Tool 数据缺失 → fallback 到 V1.2 统一 RAG（不破坏线上）
- 未登录用户 + order/refund 意图 → 不报错，返回「请登录」模板

V2.5 范围（M4）：
- 4 类意图分派
- 单步（不做 Agent 多步）
"""
import logging
import re as _re
import threading
from typing import Any, Generator, Optional, Tuple

from app.core.config import settings
from app.core.qwen import stream_chat as qwen_stream_chat
from app.services.intent_service import IntentService
from app.services.metrics import metrics  # M8
from app.services.order_service import OrderService
from app.services.policy_service import PolicyService
from app.services.refund_graph import refund_graph_app  # V3 LangGraph 版
from app.services.refund_service import RefundService
from app.services.rag.pipeline import run_stream as v12_rag_run_stream
from app.services.query_rewriter import rewrite_query  # M12：指代补全
from app.tools.product_tool import ProductTool
from app.services.session_service import ANONYMOUS_USER_ID

logger = logging.getLogger(__name__)

# §9 并发控制：P1 压测发现 50 并发直接打 LLM 触发 DashScope 限流（429）
# 用 semaphore 限流到 10 路并发，超出请求排队等待
# 实测 DashScope qwen-plus 默认 ~60 QPM，10 并发是安全水位
_LLM_SEMAPHORE = threading.Semaphore(10)


# =============================================================
# Prompt 模板（PROJECT_DESIGN §7 硬约束：tool > policy > product > history）
# =============================================================
SYSTEM_PROMPT_BASE = (
    "你是一个专业的电商客服助手。"
    "请严格基于以下【结构化数据】和【参考资料】回答用户问题。"
    "如果信息不足，请直接回答「我不知道」并建议联系人工客服，不要编造。"
    "回答要简洁、准确，必要时引用订单号、价格、政策条款编号。"
    "回答控制在 200 字以内，不要长篇大论，先给结论再补充细节。"
)

# 模板：未登录 + 需要 user 上下文的意图
NO_LOGIN_PROMPT = (
    "用户尚未登录，无法查询个人订单/退款信息。"
    "请礼貌引导用户登录后再来查询。"
)


def _build_context_block(sku: Optional[str], order_no: Optional[str], user_id: Optional[int]) -> str:
    """M9.5：构建从商品/订单跳转携带的 context 信息（注入 LLM prompt）

    让 LLM 知道用户当前在问哪个商品/哪个订单，避免"您问的是哪款"反问。

    Returns:
        多行字符串，每行一个 context 段；无 context 时返回空串。
    """
    lines = []
    # 商品 context
    if sku:
        try:
            products = ProductTool.list_products(category=None, limit=100)
            product = next((p for p in products if p.get("sku") == sku), None)
            if product:
                attrs = product.get("attributes") or {}
                attrs_str = "、".join(f"{k}={v}" for k, v in attrs.items()) if isinstance(attrs, dict) else ""
                lines.append(
                    f"【当前商品】SKU={product['sku']} | 名称={product['name']} | "
                    f"价格=¥{product['price']} | 库存={product.get('stock', '?')}"
                    + (f" | 规格={attrs_str}" if attrs_str else "")
                )
            else:
                lines.append(f"【当前商品】SKU={sku}（未在售/已下架）")
        except Exception as e:
            logger.warning(f"加载商品 context 失败 sku={sku}: {e}")
            lines.append(f"【当前商品】SKU={sku}")

    # 订单 context（必须登录且属于本人）
    if order_no and user_id and user_id != ANONYMOUS_USER_ID:
        try:
            order = OrderService.get_order_detail(user_id, order_no)
            if order:
                items = order.get("items", [])
                items_str = "、".join(f"{it['product_name']}×{it['qty']}" for it in items[:3])
                if len(items) > 3:
                    items_str += f" 等{len(items)}件"
                logi = order.get("logistics") or {}
                logi_str = ""
                if logi:
                    logi_str = f" | 物流={logi.get('logistics_no', '?')} ({logi.get('status', '')}@{logi.get('last_location', '')})"
                lines.append(
                    f"【当前订单】订单号={order_no} | 状态={order.get('status', '?')} | "
                    f"商品={items_str} | 金额=¥{order.get('total_amount', '?')}{logi_str}"
                )
            else:
                lines.append(f"【当前订单】订单号={order_no}（不存在或不属于当前用户）")
        except Exception as e:
            logger.warning(f"加载订单 context 失败 order_no={order_no}: {e}")
            lines.append(f"【当前订单】订单号={order_no}")

    return "\n".join(lines)

# 模板：通用 chat 模板（order/refund 已有 tool_result 后用）
def _build_chat_prompt(
    *,
    intent: str,
    tool_block: str,
    policy_block: str,
    product_block: str,
    history_block: str,
    query: str,
    context_block: str = "",
) -> str:
    """组装 chat 模板（按 §7 优先级硬约束）

    M9.5：context_block（来自 sku/order_no 跳转）作为最高优先级注入
    """
    sections = []
    if context_block:
        # M9.5：context（用户当前在看的商品/订单）放在最前面，LLM 第一时间知道是哪款/哪个订单
        sections.append(f"【当前场景】(M9.5 用户跳转 context)\n{context_block}")
    if tool_block:
        sections.append(f"【事实陈述】(最高优先级)\n{tool_block}")
    if policy_block:
        sections.append(f"【政策依据】\n{policy_block}")
    if product_block:
        sections.append(f"【商品知识】\n{product_block}")
    if history_block:
        sections.append(f"【对话历史】\n{history_block}")
    if not sections:
        sections.append("（无可用资料）")
    sections.append(f"问题：{query}")
    return "\n\n".join(sections)


def _format_tool_result(intent: str, tool_result: Optional[dict]) -> str:
    """把 tool 输出格式化成中文自然语言片段（供 LLM 引用）"""
    if not tool_result:
        return ""
    # order_query: 列表 / 详情
    if intent == "order_query":
        if "orders" in tool_result:
            orders = tool_result.get("orders", [])
            if not orders:
                return "用户当前没有订单。"
            lines = [f"用户共有 {len(orders)} 笔订单："]
            for o in orders:
                lines.append(
                    f"- 订单号 {o.get('order_no')} | 状态 {o.get('status')} | "
                    f"金额 ¥{o.get('total_amount')} | 下单时间 {o.get('create_time')}"
                )
            return "\n".join(lines)
        if "order" in tool_result:
            o = tool_result.get("order", {})
            items = tool_result.get("items", [])
            logistics = tool_result.get("logistics", {})
            lines = [
                f"订单号 {o.get('order_no')} | 状态 {o.get('status')} | 金额 ¥{o.get('total_amount')}",
                f"明细：{len(items)} 件商品",
            ]
            for it in items:
                lines.append(f"  - {it.get('product_name')} × {it.get('qty')} = ¥{it.get('subtotal')}")
            if logistics:
                lines.append(
                    f"物流：{logistics.get('status')} | 最新位置 {logistics.get('last_location')} | 单号 {logistics.get('logistics_no')}"
                )
            return "\n".join(lines)
    # refund_query
    if intent == "refund_query":
        tr = tool_result.get("tool_result", tool_result) if "tool_result" in tool_result else tool_result
        if tr.get("refundable") is not None:
            reason = tr.get("reason", "")
            return (
                f"退款判断：{'可退' if tr['refundable'] else '不可退'} | 原因：{reason} | "
                f"订单状态 {tr.get('order_status')} | 已签收 {tr.get('days_since_order')} 天"
            )
    return str(tool_result)


def _format_policy_docs(docs: list[dict]) -> str:
    """把 policy RAG 结果格式化"""
    if not docs:
        return ""
    lines = []
    for i, d in enumerate(docs, 1):
        text = (d.get("text") or "").strip()
        if text:
            lines.append(f"[{i}] {text[:500]}{'...' if len(text) > 500 else ''}")
    return "\n".join(lines)


def _format_history(history: Optional[list[dict]]) -> str:
    """复用 pipeline 风格的历史格式"""
    if not history:
        return ""
    lines = []
    for msg in history:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            lines.append(f"用户：{content}")
        elif role == "assistant":
            lines.append(f"助手：{content}")
    return "\n".join(lines)


# M9.5+：从历史消息中提取最近一个订单号（ORD + 8位日期 + 3位序号）
_ORDER_NO_RE = _re.compile(r"ORD\d{8}\d{3}")


def _extract_order_no_from_history(history: Optional[list[dict]]) -> Optional[str]:
    """从对话历史里提取最近一个出现的订单号

    用于多轮对话：用户第一轮 "ORD20260628004 啥情况"，第二轮只说 "那能退吗"
    时，refund handler 能从 history 自动补上 order_no。

    扫描规则：从最新消息往前扫，user 和 assistant 消息都看，
    返回第一个匹配 ORD + 8位日期 + 3位序号 的字符串。
    """
    if not history:
        return None
    # history 通常按时间正序（最旧 → 最新），所以反向遍历找最近一个
    for msg in reversed(history):
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        m = _ORDER_NO_RE.search(content)
        if m:
            return m.group(0)
    return None


# =============================================================
# Synthesizer 入口
# =============================================================
class Synthesizer:
    """多源融合层（M4）"""

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
        context_block = _build_context_block(sku, order_no, user_id)
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
            yield from Synthesizer._stream_simple(NO_LOGIN_PROMPT)
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
            yield from Synthesizer._stream_simple(direct)
            return

        if effective_order_no:
            detail = OrderService.get_order_detail(user_id, effective_order_no)
            if not detail:
                tool_block = f"订单 {effective_order_no} 不存在或不属于当前用户。"
            else:
                tool_block = _format_tool_result("order_query", detail)
        else:
            # 无 order_no → 列最近订单（OrderService.list_user_orders 不支持 limit，按默认上限返回）
            orders = OrderService.list_user_orders(user_id)
            tool_block = _format_tool_result("order_query", {"orders": orders})

        meta = {
            "intent": "order_query",
            "entities": entities,
            "contexts": [],
            "scores": [],
            "tool_result_preview": tool_block[:200] if tool_block else "",
        }
        yield ("meta", meta)

        prompt = _build_chat_prompt(
            intent="order_query",
            tool_block=tool_block,
            policy_block="",
            product_block="",
            history_block="",
            query=query,
            context_block=context_block,
        )
        yield from Synthesizer._stream_llm(prompt)

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
            yield from Synthesizer._stream_simple(NO_LOGIN_PROMPT)
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
                yield from Synthesizer._stream_simple("用户当前没有订单，无法判断退款。请提供订单号。")
                return

        result = RefundService.check_refundable_with_policy(user_id, effective_order_no, query)
        tool_block = _format_tool_result("refund_query", result)
        policy_docs = result.get("policy_docs", [])
        policy_block = _format_policy_docs(policy_docs)

        meta = {
            "intent": "refund_query",
            "entities": entities,
            "contexts": [],
            "scores": [],
            "order_no": effective_order_no,
            "refundable": result.get("tool_result", {}).get("refundable"),
            "policy_hits": len(policy_docs),
        }
        yield ("meta", meta)

        prompt = _build_chat_prompt(
            intent="refund_query",
            tool_block=tool_block,
            policy_block=policy_block,
            product_block="",
            history_block="",
            query=query,
            context_block=context_block,
        )
        yield from Synthesizer._stream_llm(prompt)

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
            or _extract_order_no_from_history(history)
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
            yield from Synthesizer._stream_simple(NO_LOGIN_PROMPT)
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
            yield from Synthesizer._stream_simple(
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
                products = Synthesizer._search_by_keyword_window(query, limit=5)

        # 格式化 product 块
        if not products:
            product_block = "未在数据库中找到相关商品。"
        else:
            lines = []
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
        kb_block = _format_policy_docs(kb_docs)
        if kb_block:
            if product_block and product_block != "未在数据库中找到相关商品。":
                product_block = f"{product_block}\n\n【商品详细规格（来自知识库）】\n{kb_block}"
            else:
                product_block = f"未在数据库中找到相关商品。\n\n【知识库相关参考】\n{kb_block}"

        meta = {
            "intent": "product_query",
            "entities": entities,
            "contexts": [],
            "scores": [],
            "products_found": len(products),
            "kb_hits": len(kb_docs),
        }
        yield ("meta", meta)

        prompt = _build_chat_prompt(
            intent="product_query",
            tool_block="",
            policy_block="",
            product_block=product_block,
            history_block=_format_history(history),
            query=query,
            context_block=context_block,
        )
        yield from Synthesizer._stream_llm(prompt)

    @staticmethod
    def _handle_policy(
        query: str, intent_result: dict, history: Optional[list[dict]],
        context_block: str = "",
    ) -> Generator[Tuple[str, Any], None, None]:
        """policy_query：纯 PolicyService RAG（最接近 V1.2 行为）"""
        policy_docs = PolicyService.search_policy(query, top_k=5)
        policy_block = _format_policy_docs(policy_docs)

        meta = {
            "intent": "policy_query",
            "entities": intent_result["entities"],
            "contexts": [],
            "scores": [],
            "policy_hits": len(policy_docs),
        }
        yield ("meta", meta)

        if not policy_docs:
            # 无相关 policy → LLM 用通用知识兜底（也带 context 让 LLM 知道用户场景）
            ctx_section = f"\n\n【当前场景】\n{context_block}" if context_block else ""
            yield from Synthesizer._stream_llm(
                f"参考资料：\n（未检索到相关政策）{ctx_section}\n\n对话历史：\n{_format_history(history)}\n\n问题：{query}"
            )
            return

        prompt = _build_chat_prompt(
            intent="policy_query",
            tool_block="",
            policy_block=policy_block,
            product_block="",
            history_block=_format_history(history),
            query=query,
            context_block=context_block,
        )
        yield from Synthesizer._stream_llm(prompt)

    # ---------- LLM 流式辅助 ----------

    @staticmethod
    def _stream_llm(user_prompt: str) -> Generator[Tuple[str, Any], None, None]:
        """单 LLM 流式调用 + done 事件（§9 并发限流 semaphore=10）

        P1：max_tokens=512 压输出长度，省 token 也防 LLM 长篇大论
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_BASE},
            {"role": "user", "content": user_prompt},
        ]
        full_answer = ""
        # semaphore 包住整个流式调用：>10 并发时排队，超出请求首 token 延迟增大但不会 429
        with _LLM_SEMAPHORE:
            for chunk in qwen_stream_chat(messages, temperature=0.3, max_tokens=512):
                full_answer += chunk
                yield ("token", chunk)
        # M8：粗估 token 数（中文 ~1 char ≈ 1.5 token；这里简化为 char 数）
        metrics.record_answer_tokens(len(full_answer))
        yield ("done", {"answer": full_answer})

    @staticmethod
    def _search_by_keyword_window(query: str, limit: int = 5) -> list[dict]:
        """
        用滑动窗口（2-3 字）抽 query 里的实词，逐个调 ProductTool.search_by_keyword，
        命中即返回。最坏情况下 N 次调用（N = 候选数）— 接受（小数据集，前缀检查会快速失败）
        """
        import re
        seen = set()
        candidates = []
        for size in (2, 3):
            for i in range(len(query) - size + 1):
                c = query[i:i + size]
                # 只保留纯中文字段
                if re.fullmatch(r"[\u4e00-\u9fff]+", c) and c not in seen:
                    seen.add(c)
                    candidates.append(c)
        # 按出现顺序（自然语言里关键词偏后）；倒序先查"尾巴词"
        for kw in reversed(candidates):
            ps = ProductTool.search_by_keyword(kw, limit=limit)
            if ps:
                logger.info(f"product keyword window 命中: kw='{kw}' → {len(ps)} 条")
                return ps
        return []

    @staticmethod
    def _stream_simple(text: str) -> Generator[Tuple[str, Any], None, None]:
        """简单文本直接 yield（不走 LLM）— 仍按 token + done 协议，chat.py 通用累加逻辑可工作"""
        yield ("token", text)
        metrics.record_answer_tokens(len(text))  # M8
        yield ("done", {"answer": text})