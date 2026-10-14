"""
XianyuTipParser V16.3 — 闲鱼订单事件解析 + 落库

设计要点：
- 5 态订单事件：paid / shipped / signed / received / refunded
- 自动从 textCard 提取订单号（fleamarket://order_detail?id=...）
- 自动落库到 xianyu_orders 表（幂等 + 状态机）
- 不依赖 LLM，0 成本 + 0 延迟

调用方：
  ReplyScheduler.on_fire → XianyuTipParser.parse_and_persist(db, seller_unb, messages)
    → list[XianyuOrder]（落库结果）
"""
from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from app.models.xianyu_order import XianyuOrder
from app.services.xianyu.order_repository import XianyuOrderRepository

logger = logging.getLogger(__name__)


class OrderStatus(str, Enum):
    """5 态订单状态"""

    PAID = "paid"          # 已付款
    SHIPPED = "shipped"    # 已发货
    SIGNED = "signed"      # 已签收
    RECEIVED = "received"  # 已收货
    REFUNDED = "refunded"  # 已退款


# 状态 → pattern 列表（顺序：精确匹配优先）
_STATUS_PATTERNS: dict[OrderStatus, list[str]] = {
    # 优先级最高：refunded（最关键 + 状态唯一）
    OrderStatus.REFUNDED: [
        "退款成功",
        "钱款已原路退返",
        "原路退回",
        "已退款",
        "退款已完成",
        "退款到账",
    ],
    OrderStatus.SIGNED: [
        "订单已签收",
        "已签收",
    ],
    OrderStatus.SHIPPED: [
        "卖家已发货",
        "已发货",
        "商品已寄出",
    ],
    OrderStatus.RECEIVED: [
        "你已确认收货",
        "交易成功",
        "已收货",
    ],
    OrderStatus.PAID: [
        "我已付款",
        "订单已创建",
        "等待卖家发货",
        "付款成功",
    ],
}

# 订单号正则（textCard 链接中提取）
# 订单号可能是纯数字（4303894788218384306）或字母+数字（O1）
# 用 [\w-]+ 兼容两种（\w = 字母+数字+下划线）
_ORDER_NO_PATTERN = re.compile(r"order_detail\?id=([\w-]+)")
# 备用：订单号：xxxx
_ORDER_NO_PATTERN_2 = re.compile(r"订单号[：:]\s*([\w-]+)")


def _match_status(text: str) -> Optional[OrderStatus]:
    """在文本中匹配订单状态（按优先级 refunded > signed > ...）"""
    if not text:
        return None
    # 按优先级顺序匹配（refunded 第一个）
    for status in [
        OrderStatus.REFUNDED,
        OrderStatus.SIGNED,
        OrderStatus.SHIPPED,
        OrderStatus.RECEIVED,
        OrderStatus.PAID,
    ]:
        for pattern in _STATUS_PATTERNS[status]:
            if pattern in text:
                return status
    return None


def _extract_item_id(content: str) -> Optional[str]:
    """从 textCard.content / tip 提 item_id（fleamarket://item?id=...）"""
    if not content:
        return None
    m = re.search(r"item\?id=([\w-]+)", content)
    if m:
        return m.group(1)
    return None


def _extract_order_no(content: str) -> Optional[str]:
    """从 textCard.content / tip 提订单号"""
    if not content:
        return None
    m = _ORDER_NO_PATTERN.search(content)
    if m:
        return m.group(1)
    m = _ORDER_NO_PATTERN_2.search(content)
    if m:
        return m.group(1)
    return None


def _extract_tip_text(message: dict) -> str:
    """从 message dict 提 tip 文本"""
    msg = message.get("message") or {}
    if not isinstance(msg, dict):
        return ""
    tip = msg.get("tip") or {}
    if isinstance(tip, dict):
        return tip.get("tip", "") or ""
    return ""


def _extract_text_text(message: dict) -> str:
    """从 message dict 提 text 文本（用于"我已付款"等买家发的文本）"""
    msg = message.get("message") or {}
    if not isinstance(msg, dict):
        return ""
    text = msg.get("text") or {}
    if isinstance(text, dict):
        return text.get("text", "") or ""
    return ""


def _extract_textcard_content(message: dict) -> str:
    """从 message dict 提 textCard.content"""
    msg = message.get("message") or {}
    if not isinstance(msg, dict):
        return ""
    tc = msg.get("textCard") or {}
    if isinstance(tc, dict):
        return tc.get("content", "") or ""
    return ""


class XianyuTipParser:
    """闲鱼订单事件解析 + 落库（V16.3）"""

    @staticmethod
    def parse_tip(text: str) -> Optional[OrderStatus]:
        """从纯文本中匹配订单状态（用于测试 + 单独使用）"""
        return _match_status(text)

    @staticmethod
    def extract_order_no(content: str) -> Optional[str]:
        """从 textCard.content 提订单号（用于测试 + 单独使用）"""
        return _extract_order_no(content)

    @staticmethod
    def parse_and_persist(
        db: Session, seller_unb: str, messages: list[dict]
    ) -> list[XianyuOrder]:
        """从消息历史解析订单事件 + 落库

        Args:
            db: SQLAlchemy Session
            seller_unb: 卖家（cookie 账号）unb
            messages: 闲鱼 message history

        Returns:
            落库成功的 XianyuOrder 列表（每条订单一行，可能含多条订单）

        跳过规则：
        1. 无 unb 的消息（系统消息）
        2. 无订单号（order_no）的消息（必须 textCard 里有 order_detail 链接）
        3. 状态无法识别的（无 tip pattern 命中）
        """
        results: list[XianyuOrder] = []
        last_order_by_key: dict[tuple[str, str], XianyuOrder] = {}
        # 缓存 unb → order_no 的最近映射（订单号只在 textCard 里出现一次）
        last_order_no_by_unb: dict[str, str] = {}
        # 缓存 unb → item_id（item_id 也在 textCard 出现）
        last_item_id_by_unb: dict[str, str] = {}

        for m in messages or []:
            unb = m.get("send_user_id", "")
            if not unb:
                continue
            msg = m.get("message") or {}
            if not isinstance(msg, dict):
                continue

            # 合并 tip + text + textCard 文本
            tip_text = _extract_tip_text(m)
            text_text = _extract_text_text(m)
            textcard_content = _extract_textcard_content(m)
            combined = f"{tip_text} {text_text} {textcard_content}".strip()

            # 1. 识别状态
            new_status = _match_status(combined)
            if new_status is None:
                continue

            # 2. 提订单号（textCard 优先；同 unb 最近一次见到的也认）
            order_no = _extract_order_no(textcard_content) or _extract_order_no(combined)
            if not order_no:
                # 兜底：用同 unb 最近一次见到的订单号
                order_no = last_order_no_by_unb.get(unb)
            if not order_no:
                logger.debug(
                    f"XianyuTipParser 跳过：无 order_no unb={unb} tip={tip_text!r}"
                )
                continue
            # 记忆：这条消息的订单号
            last_order_no_by_unb[unb] = order_no

            # 3. 提 item_id（textCard 里有 → 记忆；没 → 用上次的）
            new_item_id = _extract_item_id(textcard_content) or _extract_item_id(combined)
            if new_item_id:
                last_item_id_by_unb[unb] = new_item_id
            item_id = new_item_id or last_item_id_by_unb.get(unb)

            # 4. 落库（每次事件都调，让状态机升级生效）
            order = XianyuOrderRepository.upsert_from_tip(
                db=db,
                seller_unb=seller_unb,
                unb=unb,
                item_id=item_id,
                order_no=order_no,
                new_status=new_status.value,
                tip_source=combined[:500],
                raw_message_id=m.get("message_id", ""),
            )
            last_order_by_key[(unb, order_no)] = order

        return list(last_order_by_key.values())


__all__ = ["XianyuTipParser", "OrderStatus"]