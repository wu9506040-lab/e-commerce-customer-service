"""用户上下文预加载服务（V15）

会话入口触发，加载：
1. 最近 N 单（含产品明细）
2. 用户画像摘要（复用 profile_service）

设计：
- 灰度开关：ENABLE_USER_CONTEXT_PRELOAD=False 时短路返空（默认关，验证后开）
- best-effort：任一字段失败不影响其他字段
- 缓存策略：调用方负责（如 ContextService.update recent_orders_summary），不在本模块

面试要点（V15 新增）：
- 用户进入会话即预加载，最近 5 单（含产品名）→ 注入 LLM prompt
- 用户问"我昨天买的商品要退款"——不用报订单号，LLM 看到 ORD20260601001 (ZP1 蓝牙耳机 / ¥99) 主动关联

§3.3 YAGNI 边界：
- 不做事件流（preload_events）
- 不做 TTL 清理（按 conversations.deleted 跟随）
- 不做租户级画像（user_id 1:1）
"""
from __future__ import annotations

import logging
from typing import Optional

from app.services.order_service import OrderService
from app.services import profile_service
from app.services.session_service import ANONYMOUS_USER_ID

logger = logging.getLogger(__name__)


# === 配置常量 ===
MAX_RECENT_ORDERS: int = 5  # 预加载订单上限（防 prompt 膨胀）
MAX_ITEMS_PER_ORDER: int = 3  # 每单最多展示几个商品名
MAX_PROMPT_LEN: int = 300  # 整个 user_context_block 字符硬上限


# =============================================================
# 数据预加载（best-effort，单字段失败不影响整体）
# =============================================================
def preload(user_id: int) -> dict:
    """
    加载用户会话级上下文（订单 + 画像）。

    Args:
        user_id: 用户 ID（None/0/匿名 → 返空 dict）

    Returns:
        {
            "orders": [精简订单列表],
            "profile_text": str,  # profile_service.to_prompt_block 输出
        }
        任一字段失败 → 该字段为空，不抛
    """
    if not user_id or user_id == ANONYMOUS_USER_ID:
        return {"orders": [], "profile_text": ""}

    result = {"orders": [], "profile_text": ""}

    # 1. 最近订单（含产品明细）
    try:
        orders = OrderService.list_user_orders(user_id, limit=MAX_RECENT_ORDERS)
        simplified = []
        for o in orders[:MAX_RECENT_ORDERS]:
            order_no = o.get("order_no")
            if not order_no:
                continue
            # 单独拿明细（OrderService.list_user_orders with_items 是 noop，走 get_order_detail）
            items_str = ""
            try:
                detail = OrderService.get_order_detail(user_id, order_no)
                if detail and detail.get("items"):
                    items_str = _simplify_items(detail["items"])
            except Exception as e:
                logger.warning(f"preload.get_order_detail 失败（继续）: order_no={order_no}, {e}")
            simplified.append({
                "order_no": order_no,
                "status": o.get("status", ""),
                "status_zh": o.get("status_zh", o.get("status", "")),
                "total_amount": o.get("total_amount"),
                "items_text": items_str,
            })
        result["orders"] = simplified
    except Exception as e:
        logger.warning(f"preload.list_user_orders 失败（放行）: user_id={user_id}, {e}")

    # 2. 用户画像（复用已有 profile_service）
    try:
        profile = profile_service.get_or_create(user_id)
        if profile:
            result["profile_text"] = profile_service.to_prompt_block(profile) or ""
    except Exception as e:
        logger.warning(f"preload.profile 失败（放行）: user_id={user_id}, {e}")

    return result


def _simplify_items(items: list, max_items: int = MAX_ITEMS_PER_ORDER) -> str:
    """把 items list 精简成 "ZP1 蓝牙耳机×1 / ZP2 鼠标×1" 形式"""
    parts = []
    for it in items[:max_items]:
        name = it.get("product_name", "?")
        qty = it.get("qty", 1)
        parts.append(f"{name}×{qty}")
    if len(items) > max_items:
        parts.append(f"等{len(items)}件")
    return " / ".join(parts)


# =============================================================
# 格式化（注入 LLM prompt）
# =============================================================
def to_prompt_block(preload_data: dict, max_len: int = MAX_PROMPT_LEN) -> str:
    """
    把预加载数据转成可注入 LLM 的 prompt 文本块。

    Args:
        preload_data: preload() 输出
        max_len: 块最大字符数（防膨胀，默认 300）

    Returns:
        多行字符串；preload_data 为空时返 ""

    输出示例：
        【会话上下文预加载】(用户最近状态，仅作参考，不要主动引用未确认信息)
        - 最近订单：ORD20260601001(已发货/¥99/ZP1 蓝牙耳机×1); ORD20260605002(已完成/¥128/ZP2 鼠标×1)
        【当前用户画像】(跨 session 长程记忆，仅作参考)
        - 偏好：preferred_category=电子产品
        - 最近提过的商品：ZP1 / ZP2
    """
    if not preload_data:
        return ""

    lines = []

    # 1. 最近订单
    orders = preload_data.get("orders") or []
    if orders:
        order_lines = []
        for o in orders:
            items_text = o.get("items_text") or "（无明细）"
            amount = o.get("total_amount")
            amount_str = f"¥{amount:.0f}" if isinstance(amount, (int, float)) else "?"
            order_lines.append(
                f"{o['order_no']}({o.get('status_zh', '?')}/{amount_str}/{items_text})"
            )
        lines.append(f"- 最近订单（最近 {len(orders)} 单）：{'; '.join(order_lines)}")

    # 2. 用户画像（profile_service 已有"【当前用户画像】"前缀，直接拼接）
    profile_text = preload_data.get("profile_text") or ""
    if profile_text:
        lines.append(profile_text)

    if not lines:
        return ""

    # 反幻觉 hard label（与现有 M9.5 prompt 模式一致）
    block = "【会话上下文预加载】(用户最近状态，仅作参考，不要主动引用未确认信息)\n" + "\n".join(lines)

    # 硬上限截断（防 prompt 膨胀）
    if len(block) > max_len:
        block = block[:max_len - 1] + "…"
    return block
