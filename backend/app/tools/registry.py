"""
Tools Layer · Registry — Tool Protocol + 静态注册 + OpenAI FC schema 转换

按 CLAUDE.md §9.3.1：每个核心模块提供输入/输出契约。
按 §9.7 自检 #3：Protocol 先于实现（本模块顶部定义 ToolSpec，REGISTRY 用之实例化）。

模块职责：
1. ToolSpec 数据类（Protocol 角色）：描述一个工具（name/description/parameters/runner）
2. ToolContext：用户级上下文（user_id 等"全会话同值"字段，避免改工具签名）
3. REGISTRY：静态注册表（key=tool_name, value=ToolSpec）
4. to_openai_tools()：ToolSpec[] → OpenAI FC tools=[] JSON Schema 转换
5. dispatch()：按 name 路由 + 参数解析 + runner 调用 + 异常容错

跨模块边界（CLAUDE.md §9.2.2）：
- 本模块**只**做注册 + 转换 + 路由；不实现业务逻辑
- 工具实现仍归 OrderTool/ProductTool/RefundTool 等具体类
- agent_runner 通过 dispatch() 间接调用，禁止直接 import 具体 Tool 类

跨循环依赖处理（§9.2.3）：
- _run_xxx 函数体内延迟 import 具体 Tool 类，避免 registry ↔ tool 互引用
"""
from dataclasses import dataclass
from typing import Callable, Optional, Any
import json
import logging

logger = logging.getLogger(__name__)


# =============================================================
# 1. ToolContext（用户级上下文）
# =============================================================
@dataclass
class ToolContext:
    """工具调用上下文（用户身份等需要"全会话同值"的字段）。

    runner 通过 ctx 拿 user_id，避免工具自己接受 user_id 显式入参
    （OpenAI FC 没有"隐藏参数"概念，必须注入到 arguments 或 ctx）。

    Why dataclass：
        - 业务层无侵入（不要求 OrderTool.get_order_by_no 等改签名）
        - 后续可加 request_id / tenant_id 等（按 CLAUDE.md §9.4.3 多租户预留）
    """
    user_id: Optional[int] = None


# =============================================================
# 2. ToolSpec（注册描述 · Protocol 角色）
# =============================================================
@dataclass
class ToolSpec:
    """单个工具的注册描述（同时承担 Protocol 角色）。

    Attributes:
        name: 工具名（必须与 OpenAI FC 的 function.name 一致 + 与 REGISTRY key 一致）
        description: 工具语义描述（写给 LLM 看的，必须清晰；LLM 据此判断调不调）
        parameters: JSON Schema 字典（OpenAI FC 的 function.parameters 字段）
        runner: 实际执行函数；签名 (args: dict, ctx: ToolContext) -> dict
    """
    name: str
    description: str
    parameters: dict
    runner: Callable[[dict, ToolContext], dict]


# =============================================================
# 3. Registry（静态注册表）
# =============================================================
def _run_lookup_order(args: dict, ctx: ToolContext) -> dict:
    """lookup_order: 查订单详情（含商品、金额、物流）。"""
    # 延迟 import 避免 registry ↔ order_tool 循环依赖
    from app.tools.order_tool import OrderTool

    order_no = args.get("order_no")
    if not order_no:
        return {"error": "order_no is required"}
    if ctx.user_id is None or ctx.user_id == 0:
        return {"error": "anonymous user cannot query order"}
    detail = OrderTool.get_order_by_no(ctx.user_id, order_no)
    if not detail:
        return {"error": f"order {order_no} not found or not owned by user"}
    return detail


def _run_search_product(args: dict, ctx: ToolContext) -> dict:
    """search_product: 按关键词搜商品。"""
    from app.tools.product_tool import ProductTool

    keyword = args.get("keyword")
    if not keyword:
        return {"error": "keyword is required"}
    limit = int(args.get("limit", 5))
    products = ProductTool.search_by_keyword(keyword, limit=limit)
    return {"products": products}


def _run_search_policy(args: dict, ctx: ToolContext) -> dict:
    """search_policy: 政策 RAG 检索（Qdrant）。"""
    from app.services.policy_service import PolicyService

    query = args.get("query")
    if not query:
        return {"error": "query is required"}
    top_k = int(args.get("top_k", 5))
    docs = PolicyService.search_policy(query, top_k=top_k)
    return {"policy_docs": docs}


def _run_check_refundable(args: dict, ctx: ToolContext) -> dict:
    """check_refundable: 判断指定订单能否退款（纯规则，不调 LLM）。"""
    # 延迟 import 避免 registry ↔ refund_tool 循环依赖
    from app.tools.refund_tool import RefundTool

    order_no = args.get("order_no")
    if not order_no:
        return {"error": "order_no is required"}
    if ctx.user_id is None or ctx.user_id == 0:
        return {"error": "anonymous user cannot check refund"}
    return RefundTool.check_refundable(ctx.user_id, order_no)


# =============================================================
# Sprint 19 · 售后 3 个 Tool（AfterSalesTool · 只读）
# =============================================================
def _run_get_refund_reason_advice(args: dict, ctx: ToolContext) -> dict:
    """get_refund_reason_advice: 退款原因填写指导。"""
    from app.tools.after_sales_tool import AfterSalesTool

    user_id = ctx.user_id
    order_no = args.get("order_no")
    reason_category = args.get("reason_category")
    if not order_no or not reason_category:
        return {"error": "order_no and reason_category are required"}
    if not user_id:
        return {"error": "user_id is required"}
    return AfterSalesTool.get_refund_reason_advice(user_id, order_no, reason_category)


def _run_get_shipping_insurance_info(args: dict, ctx: ToolContext) -> dict:
    """get_shipping_insurance_info: 运费险规则。"""
    from app.tools.after_sales_tool import AfterSalesTool

    order_no = args.get("order_no")
    return_status = args.get("return_status")
    if not order_no or not return_status:
        return {"error": "order_no and return_status are required"}
    return AfterSalesTool.get_shipping_insurance_info(order_no, return_status)


def _run_get_refund_type_advice(args: dict, ctx: ToolContext) -> dict:
    """get_refund_type_advice: 仅退款 vs 退货退款建议。"""
    from app.tools.after_sales_tool import AfterSalesTool

    user_id = ctx.user_id
    order_no = args.get("order_no")
    if not order_no:
        return {"error": "order_no is required"}
    if not user_id:
        return {"error": "user_id is required"}
    return AfterSalesTool.get_refund_type_advice(user_id, order_no)


# =============================================================
# Sprint 19 · 售前 3 个 Tool（PromotionTool · 只读）
# =============================================================
def _run_get_active_promotions(args: dict, ctx: ToolContext) -> dict:
    """get_active_promotions: 当前用户可用优惠活动。"""
    from app.tools.promotion_tool import PromotionTool

    user_id = ctx.user_id or 0  # V2 售前不强制 user_id（售前不查订单）
    cart_items = args.get("cart_items") or []
    return PromotionTool.get_active_promotions(user_id, cart_items)


def _run_check_coupon_stackable(args: dict, ctx: ToolContext) -> dict:
    """check_coupon_stackable: 优惠券叠加校验。"""
    from app.tools.promotion_tool import PromotionTool

    coupon_ids = args.get("coupon_ids") or []
    if not isinstance(coupon_ids, list) or not coupon_ids:
        return {"error": "coupon_ids must be a non-empty list"}
    return PromotionTool.check_coupon_stackable(coupon_ids)


def _run_calculate_bundle_discount(args: dict, ctx: ToolContext) -> dict:
    """calculate_bundle_discount: 跨店满减计算。"""
    from app.tools.promotion_tool import PromotionTool

    store_totals = args.get("store_totals") or {}
    if not isinstance(store_totals, dict) or not store_totals:
        return {"error": "store_totals must be a non-empty dict"}
    return PromotionTool.calculate_bundle_discount(store_totals)


# =============================================================
# Sprint 19 · 售中 3 个 Tool（OrderModifyTool · 写操作 2 步确认）
# =============================================================
def _run_modify_address(args: dict, ctx: ToolContext) -> dict:
    """modify_address: 修改收货地址（写操作；首次 confirmed=false 返 needs_confirmation）。"""
    from app.tools.order_modify_tool import OrderModifyTool

    user_id = ctx.user_id
    order_no = args.get("order_no")
    new_address = args.get("new_address")
    confirmed = bool(args.get("confirmed", False))  # 默认 False 触发确认
    if not order_no or not new_address:
        return {"error": "order_no and new_address are required"}
    if not user_id:
        return {"error": "user_id is required"}
    return OrderModifyTool.modify_address(user_id, order_no, new_address, confirmed)


def _run_modify_item_spec(args: dict, ctx: ToolContext) -> dict:
    """modify_item_spec: 修改商品规格/数量（写操作；需 confirmed=true 才执行）。"""
    from app.tools.order_modify_tool import OrderModifyTool

    user_id = ctx.user_id
    order_no = args.get("order_no")
    sku = args.get("sku")
    new_qty = args.get("new_qty")  # 可选（None 表示不调整数量）
    confirmed = bool(args.get("confirmed", False))
    if not order_no or not sku:
        return {"error": "order_no and sku are required"}
    if not user_id:
        return {"error": "user_id is required"}
    return OrderModifyTool.modify_item_spec(user_id, order_no, sku, new_qty, confirmed)


def _run_merge_orders(args: dict, ctx: ToolContext) -> dict:
    """merge_orders: 合并订单（写操作；需 confirmed=true 才执行）。"""
    from app.tools.order_modify_tool import OrderModifyTool

    user_id = ctx.user_id
    order_nos = args.get("order_nos") or []
    confirmed = bool(args.get("confirmed", False))
    if not isinstance(order_nos, list) or len(order_nos) < 2:
        return {"error": "order_nos must be a list of at least 2 order numbers"}
    if not user_id:
        return {"error": "user_id is required"}
    return OrderModifyTool.merge_orders(user_id, order_nos, confirmed)


REGISTRY: dict[str, ToolSpec] = {
    "lookup_order": ToolSpec(
        name="lookup_order",
        description="查询指定订单号的订单详情（含商品、金额、物流）。"
                    "用户询问订单状态/物流/金额时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "订单号，例如 'ORD20240101001'",
                },
            },
            "required": ["order_no"],
        },
        runner=_run_lookup_order,
    ),
    "search_product": ToolSpec(
        name="search_product",
        description="按关键词搜索商品（SKU / 名称 / 类目）。"
                    "用户问商品信息（颜色、库存、价格、规格）时调用。",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，可以是 SKU、商品名或类目",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回商品数量上限，默认 5",
                    "default": 5,
                },
            },
            "required": ["keyword"],
        },
        runner=_run_search_product,
    ),
    "search_policy": ToolSpec(
        name="search_policy",
        description="在电商知识库中检索相关政策（退货、运费、保修、发票等）。"
                    "用户问政策类问题时调用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词（用户原始问题或简化版）",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回文档数量上限，默认 5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        runner=_run_search_policy,
    ),
    "check_refundable": ToolSpec(
        name="check_refundable",
        description="判断指定订单能否退款（已签收 7 天内 / 其他状态均可，已退款或超期不可退）。"
                    "用户问'能不能退'/'还能退吗'/'还在退款期吗'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "订单号，例如 'ORD20240101001'",
                },
            },
            "required": ["order_no"],
        },
        runner=_run_check_refundable,
    ),

    # =============================================================
    # Sprint 19 · 售后 3（只读）
    # =============================================================
    "get_refund_reason_advice": ToolSpec(
        name="get_refund_reason_advice",
        description="退款原因填写指导：建议具体原因文字 + 需要的凭证 + 成功率提示。"
                    "用户问'怎么填退款原因'/'什么理由容易过'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "reason_category": {
                    "type": "string",
                    "enum": ["quality", "no_reason", "size", "not_as_described", "late", "other"],
                    "description": "原因类别",
                },
            },
            "required": ["order_no", "reason_category"],
        },
        runner=_run_get_refund_reason_advice,
    ),
    "get_shipping_insurance_info": ToolSpec(
        name="get_shipping_insurance_info",
        description="运费险规则：哪些情况赔 / 赔多少 / 多久到账。"
                    "用户问'运费险赔多少'/'运费险什么时候到'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "return_status": {
                    "type": "string",
                    "enum": ["return_shipped", "return_received", "refunded"],
                    "description": "退货状态",
                },
            },
            "required": ["order_no", "return_status"],
        },
        runner=_run_get_shipping_insurance_info,
    ),
    "get_refund_type_advice": ToolSpec(
        name="get_refund_type_advice",
        description="仅退款 vs 退货退款建议：根据订单状态 + 金额判断哪种方式更适合。"
                    "用户问'我该选哪种退款'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
            },
            "required": ["order_no"],
        },
        runner=_run_get_refund_type_advice,
    ),

    # =============================================================
    # Sprint 19 · 售前 3（只读）
    # =============================================================
    "get_active_promotions": ToolSpec(
        name="get_active_promotions",
        description="查当前用户可用的优惠活动（满减/折扣/赠品）。"
                    "用户问'有什么优惠'/'双11怎么减'/'我能用什么券'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "cart_items": {
                    "type": "array",
                    "description": "购物车商品列表 [{sku, qty, unit_price, store_id, category}, ...]，可选",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string"},
                            "qty": {"type": "integer"},
                            "unit_price": {"type": "number"},
                            "store_id": {"type": "string"},
                            "category": {"type": "string"},
                        },
                    },
                },
            },
        },
        runner=_run_get_active_promotions,
    ),
    "check_coupon_stackable": ToolSpec(
        name="check_coupon_stackable",
        description="查多张优惠券能否叠加：返回可叠加分组 + 互斥对 + 最佳组合。"
                    "用户问'我的券能一起用吗'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "coupon_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "优惠券 ID 列表",
                },
            },
            "required": ["coupon_ids"],
        },
        runner=_run_check_coupon_stackable,
    ),
    "calculate_bundle_discount": ToolSpec(
        name="calculate_bundle_discount",
        description="算跨店满减 + 凑单建议：输入每家店金额，返回当前合计 + 距离下一档 + 凑单建议。"
                    "用户问'几家店凑单怎么减'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "store_totals": {
                    "type": "object",
                    "description": "每家店金额 {storeA: 280, storeB: 150, ...}",
                    "additionalProperties": {"type": "number"},
                },
            },
            "required": ["store_totals"],
        },
        runner=_run_calculate_bundle_discount,
    ),

    # =============================================================
    # Sprint 19 · 售中 3（写操作 · 需 confirmed=true）
    # =============================================================
    "modify_address": ToolSpec(
        name="modify_address",
        description="修改订单收货地址（限未发货订单）。"
                    "**写操作前必须设置 confirmed=true**，否则会返回确认提示。"
                    "用户问'能改地址吗'/'我想改收货地址'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "new_address": {"type": "string", "description": "新收货地址"},
                "confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": "用户已确认修改（true=执行；false=返回确认提示）",
                },
            },
            "required": ["order_no", "new_address"],
        },
        runner=_run_modify_address,
    ),
    "modify_item_spec": ToolSpec(
        name="modify_item_spec",
        description="修改订单商品规格/数量（限未发货订单；V2 仅支持数量调整）。"
                    "**写操作前必须设置 confirmed=true**，否则会返回确认提示。"
                    "用户问'能改数量吗'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号"},
                "sku": {"type": "string", "description": "目标 SKU"},
                "new_qty": {
                    "type": "integer",
                    "description": "新数量（可选；None=不调整）",
                },
                "confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": "用户已确认修改（true=执行；false=返回确认提示）",
                },
            },
            "required": ["order_no", "sku"],
        },
        runner=_run_modify_item_spec,
    ),
    "merge_orders": ToolSpec(
        name="merge_orders",
        description="合并订单（限同一店铺 + 未发货 + 5 分钟内）。"
                    "**写操作前必须设置 confirmed=true**，否则会返回确认提示。"
                    "用户问'能合并订单吗'时调用。",
        parameters={
            "type": "object",
            "properties": {
                "order_nos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "待合并订单号列表（≥ 2）",
                },
                "confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": "用户已确认合并（true=执行；false=返回确认提示）",
                },
            },
            "required": ["order_nos"],
        },
        runner=_run_merge_orders,
    ),
}


# =============================================================
# 4. OpenAI FC format converter
# =============================================================
def to_openai_tools() -> list[dict]:
    """把 Registry 里的所有 ToolSpec 转 OpenAI FC 的 tools=[...] 格式。

    Returns:
        [
            {"type": "function", "function": {"name", "description", "parameters"}},
            ...
        ]
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in REGISTRY.values()
    ]


# =============================================================
# 5. dispatch（按 name 路由 + 容错）
# =============================================================
def dispatch(name: str, arguments_json: str, ctx: ToolContext) -> dict:
    """根据工具名路由到对应 runner 执行。

    Args:
        name: 工具名（必须与 REGISTRY key 一致；OpenAI FC 返回的 function.name）
        arguments_json: JSON 字符串（OpenAI FC 返回的 function.arguments 字段）
        ctx: ToolContext（含 user_id）

    Returns:
        runner 返回值（dict）。
        任何异常路径（未注册 / 参数 JSON 解析失败 / runner 抛异常）
        都返回 {"error": "..."} dict，让上游 LLM 收到错误后可重试或换工具，
        不阻断 Agent 主循环。
    """
    spec = REGISTRY.get(name)
    if spec is None:
        logger.warning(f"tool registry: 未注册工具 '{name}'")
        return {"error": f"tool '{name}' not registered"}

    # 参数解析（LLM 生成的 JSON 可能格式不严）
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        logger.warning(
            f"tool registry: 参数 JSON 解析失败 '{name}': {e}",
            extra={"tool": name},
        )
        return {"error": f"invalid arguments JSON: {str(e)}"}

    # runner 调用（异常吞掉，返 error dict 避免 Agent 循环崩溃）
    try:
        result = spec.runner(args, ctx)
        # 防御：runner 应返 dict，不是 dict 时包一层（便于 LLM 消费）
        if not isinstance(result, dict):
            result = {"result": result}
        return result
    except Exception as e:
        logger.exception(
            f"tool registry: runner '{name}' 异常: {type(e).__name__}: {e}",
            extra={"tool": name},
        )
        return {
            "error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"
        }


__all__ = [
    "ToolSpec",
    "ToolContext",
    "REGISTRY",
    "to_openai_tools",
    "dispatch",
]