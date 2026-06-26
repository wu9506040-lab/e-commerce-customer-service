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