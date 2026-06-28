"""
Intent Classifier Schema（M3 新增）

按 PROJECT_DESIGN.md §3 + §7：
- 4 类意图：order_query / refund_query / product_query / policy_query
- 返回 intent + confidence + entities + method（rule / llm / default）
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


# 4 类意图字面量（前端可枚举）
IntentType = Literal["order_query", "refund_query", "product_query", "policy_query"]


class IntentRequest(BaseModel):
    """Intent Classifier 请求"""
    query: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="用户问题",
        examples=["我的订单到哪了？"],
    )
    # 预留：上下文（M4 整合时用）
    last_intent: Optional[IntentType] = Field(
        default=None,
        description="上一轮意图（V2.6 状态记忆启用后生效）",
    )


class IntentEntities(BaseModel):
    """从 query 里抽取的实体"""
    order_no: Optional[str] = Field(default=None, description="订单号，如 ORD001")
    sku: Optional[str] = Field(default=None, description="商品 SKU，如 ZP1")
    keywords: list[str] = Field(default_factory=list, description="关键词")


class IntentResponse(BaseModel):
    """Intent Classifier 响应"""
    intent: IntentType = Field(..., description="分类结果")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="置信度（规则匹配=1.0，LLM 兜底=LLM 自评，默认=0.5）",
    )
    method: Literal["rule", "llm", "default"] = Field(
        ..., description="分类方式"
    )
    entities: IntentEntities = Field(
        default_factory=IntentEntities, description="抽取的实体"
    )