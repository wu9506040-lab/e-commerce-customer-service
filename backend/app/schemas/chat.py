"""
Chat 相关 Pydantic Schema（RAG + 多轮对话）

接口：
    POST /chat
    请求：{"query": "...", "session_id": "可选"}
    响应：{"answer": "...", "contexts": [...], "scores": [...], "session_id": "..."}
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """RAG Chat 请求"""
    query: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="用户问题（支持中英文，1-2000 字符）",
        examples=["退款流程是怎样的？"],
    )
    session_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="会话 ID（传 None 则创建新会话）",
        examples=["a1b2c3d4e5f6..."],
    )
    # M9.5：从商品详情/订单卡片跳过来时携带 context（后端注入 prompt）
    sku: Optional[str] = Field(
        default=None,
        max_length=64,
        description="当前商品 SKU（从 /shop/:sku 跳转携带）",
        examples=["ZP1"],
    )
    order_no: Optional[str] = Field(
        default=None,
        max_length=64,
        description="当前订单号（从订单卡片跳转携带）",
        examples=["ORD20260622003"],
    )


class ChatResponse(BaseModel):
    """RAG Chat 响应"""
    answer: str = Field(..., description="LLM 生成的回答")
    contexts: List[str] = Field(
        default_factory=list,
        description="检索到的知识库原文（按相似度倒序）",
    )
    scores: List[float] = Field(
        default_factory=list,
        description="对应 contexts 的余弦相似度分数",
    )
    session_id: str = Field(..., description="会话 ID（用于下一轮对话）")


class ResumeRequest(BaseModel):
    """SSE 流式中断续传请求（Sprint P2 / SSE Resume）

    客户端在 /chat 流中断后未收到 done 时调用；后端从 Redis checkpoint 恢复
    已流 prefix + 重新调 LLM 接着生成。
    """
    session_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="会话 ID（必须与原 /chat 请求一致）",
    )
    stream_id: str = Field(
        ...,
        min_length=12,
        max_length=12,
        description="流式回合 ID（从 meta 事件 stream_id 字段拿到）",
    )
    query: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="用户问题（必须与原 /chat 请求一致；不匹配返 410）",
    )
    last_event_id: Optional[int] = Field(
        default=None,
        ge=0,
        description="客户端最后收到的 SSE id（用于服务端去重校验，可选）",
    )
    sku: Optional[str] = Field(
        default=None, max_length=64,
        description="当前商品 SKU（M9.5 context 透传）",
    )
    order_no: Optional[str] = Field(
        default=None, max_length=64,
        description="当前订单号（M9.5 context 透传）",
    )