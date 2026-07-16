"""SSE Card 协议（M14 · 方案 A 选定）

按 plan §7 决策：D1 选 A — meta 事件 payload 加 card? 字段
（理由：与 SSE Resume seq 一致；前端解析器零改动；一个回合最多推 1 张卡）

注意：
- OrderCardItem 与 OrderTool._order_to_dict 字段对齐（保持单一来源）
- OrderCardPayload 走 Pydantic BaseModel 校验，避免类型漂移
- 业务流阶段（flow_stage）单独放可选字段，便于 RefundFlow / LogisticsFlow 复用
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class OrderCardItem(BaseModel):
    """OrderCard 单条订单（与 OrderTool._order_to_dict 字段对齐）"""

    order_no: str
    status: str
    total_amount: float
    create_time: Optional[str] = None
    # item_count: 由前端从 SSE 上下文补全（meta 阶段不可见 items），可空
    item_count: int = 0
    preview: Optional[str] = None  # 商品名预览


class OrderCardPayload(BaseModel):
    """SSE meta 事件携带的 card 字段"""

    type: Literal["order_list", "order_detail"]
    density: Literal["mini", "list"] = "list"
    reason: Literal["disambiguate", "proactive", "context_jump"]
    items: list[OrderCardItem] = Field(default_factory=list)
    truncated: bool = False
    # 1 个订单场景下 Resolver 已选定，可透传给前端做"详情卡"
    resolved_order_no: Optional[str] = None


# 业务流阶段枚举（与 refund_graph V3 节点对齐；扩展时同步 LangGraph）
FlowStage = Literal[
    "fetch_order",
    "judge",
    "fetch_policy",
    "check_proof",
    "escalate",
    "synthesize",
]