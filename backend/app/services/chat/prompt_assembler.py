"""
Prompt Assembler（M9.5+ · Sprint 3 拆分自 synthesizer.py）

职责：构造 LLM 输入（系统 prompt + 用户 prompt + 结构化上下文格式化）。
- 不调 LLM
- 不做 intent 分类
- 不做流式输出
- 仅文本 / 字符串层面的"格式化 + 拼接"

来源：原 synthesizer.py 模块级 7 个函数（_build_context_block / _build_chat_prompt /
_format_tool_result / _format_policy_docs / _format_history / _build_meta_contexts /
_extract_order_no_from_history）+ 2 个 prompt 常量（已迁移到 config/prompts YAML）。
"""
import logging
import re as _re
from typing import Optional

from app.services.prompt_loader import get_prompt_loader
from app.services.session_service import ANONYMOUS_USER_ID
from app.tools.product_tool import ProductTool
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)


# 系统 Prompt：Sprint 2 抽到 config/prompts/agent.yaml，Sprint 3 完全删除常量
SYSTEM_PROMPT_BASE = get_prompt_loader().load("agent")


# 未登录 + 需要 user 上下文的意图
NO_LOGIN_PROMPT = get_prompt_loader().load("no_login")


def _build_context_block(
    sku: Optional[str],
    order_no: Optional[str],
    user_id: Optional[int],
    profile_block: str = "",
) -> str:
    """M9.5：构建从商品/订单跳转携带的 context 信息（注入 LLM prompt）

    让 LLM 知道用户当前在问哪个商品/哪个订单，避免"您问的是哪款"反问。

    P2 长程记忆：扩 profile_block 参数，把跨 session 用户画像拼到 context_block 末尾。
    profile_block 来自 profile_service.to_prompt_block()，默认空串（开关关闭或匿名）。

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

    # P2 长程记忆：profile_block（来自 profile_service.to_prompt_block）
    # 优先级：M9.5 context > profile（profile 是补充信息，不应覆盖当前订单/商品）
    if profile_block:
        lines.append(profile_block)

    return "\n".join(lines)


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
    """把 tool 输出格式化成中文自然语言片段（供 LLM 引用）

    P0-LLM 溯源：每段加 [订单] / [退款] 来源标签前缀，
    让 LLM 知道这是结构化数据（DB 查的，不是 LLM 编的）
    """
    if not tool_result:
        return ""
    # order_query: 列表 / 详情
    if intent == "order_query":
        if "orders" in tool_result:
            orders = tool_result.get("orders", [])
            if not orders:
                return "[订单] 用户当前没有订单。"
            lines = ["[订单] 用户共有 {} 笔订单：".format(len(orders))]
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
                f"[订单] 订单号 {o.get('order_no')} | 状态 {o.get('status')} | 金额 ¥{o.get('total_amount')}",
                f"[订单] 明细：{len(items)} 件商品",
            ]
            for it in items:
                lines.append(f"  - {it.get('product_name')} × {it.get('qty')} = ¥{it.get('subtotal')}")
            if logistics:
                lines.append(
                    f"[订单] 物流：{logistics.get('status')} | 最新位置 {logistics.get('last_location')} | 单号 {logistics.get('logistics_no')}"
                )
            return "\n".join(lines)
    # refund_query
    if intent == "refund_query":
        tr = tool_result.get("tool_result", tool_result) if "tool_result" in tool_result else tool_result
        if tr.get("refundable") is not None:
            reason = tr.get("reason", "")
            return (
                f"[退款] 退款判断：{'可退' if tr['refundable'] else '不可退'} | 原因：{reason} | "
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


def _build_meta_contexts(
    policy_docs: Optional[list[dict]] = None,
    products: Optional[list[dict]] = None,
    tool_result: Optional[dict] = None,
) -> tuple[list[dict], list[float]]:
    """P0-H：把 RAG 检索结果 / tool 结果转成 meta contexts（暴露给前端/调试用）

    Returns:
        (contexts, scores) — contexts 是 [{source, text_preview, type}, ...]，
        scores 是对应余弦相似度（policy_docs 有 score，products/tool 没 score 用 None 占位）

    之前所有 meta 都硬编码 "contexts": []，前端看不到检索命中，调试也看不到来源
    """
    contexts: list[dict] = []
    scores: list[float] = []
    if policy_docs:
        for d in policy_docs:
            text = (d.get("text") or "").strip()
            contexts.append({
                "source": d.get("source") or d.get("payload", {}).get("source", "knowledge_base"),
                "text_preview": text[:200] + ("..." if len(text) > 200 else ""),
                "type": "policy",
            })
            score = d.get("score")
            if isinstance(score, (int, float)):
                scores.append(float(score))
    if products:
        for p in products:
            contexts.append({
                "source": f"product:{p.get('sku', '?')}",
                "text_preview": f"{p.get('name', '')} | ¥{p.get('price', '?')} | 库存 {p.get('stock', '?')}",
                "type": "product",
            })
            scores.append(0.0)  # tool 结果无 cosine 分数，用 0 占位
    if tool_result:
        # order_query 的 tool_result 是 dict / list，统一抽出关键标识
        if "order" in tool_result:
            o = tool_result["order"]
            contexts.append({
                "source": f"order:{o.get('order_no', '?')}",
                "text_preview": f"状态 {o.get('status')} | 金额 ¥{o.get('total_amount')}",
                "type": "order",
            })
        elif "orders" in tool_result:
            for o in tool_result["orders"]:
                contexts.append({
                    "source": f"order:{o.get('order_no', '?')}",
                    "text_preview": f"状态 {o.get('status')} | 金额 ¥{o.get('total_amount')}",
                    "type": "order",
                })
        scores.append(0.0)
    return contexts, scores


# M9.5+：从历史消息中提取最近一个订单号
# 订单号格式：ORD + 8位日期(YYYYMMDD) + uuid4().hex[:6].upper()（3-6位大写字母数字混合）
# M13 修复：原 regex 只匹配纯数字，遗漏了字母后缀（如 ORD20260704899EBA）
_ORDER_NO_RE = _re.compile(r"ORD\d{8}[A-Z0-9]{3,6}")


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
