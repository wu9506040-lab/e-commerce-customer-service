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
        - 业务层无侵入（不要求 OrderTool.get_order_detail 等改签名）
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
    detail = OrderTool.get_order_detail(ctx.user_id, order_no)
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