"""
Intent Classifier API（M3 新增）

按 PROJECT_DESIGN.md §7 + §8：
- 4 类意图分类（规则优先 + LLM 兜底）
- 性能目标：规则命中 < 100ms；LLM 兜底 1-2s
- 接入点：独立端点，不动 /chat（M4 整合）

端点：
    POST /intent/classify
    请求：{"query": "...", "last_intent": "可选"}
    响应：{"intent": "...", "confidence": 0.95, "method": "rule|llm|default", "entities": {...}}
"""
import logging

from fastapi import APIRouter

from app.schemas.intent import IntentRequest, IntentResponse
from app.services.intent_service import IntentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/intent", tags=["intent"])


@router.post(
    "/classify",
    response_model=IntentResponse,
    summary="意图分类",
    description="M3 独立端点：4 类意图分类。规则优先，LLM 兜底，默认 policy_query。",
)
async def classify_intent(payload: IntentRequest) -> IntentResponse:
    """分类用户问题意图"""
    result = IntentService.classify(
        query=payload.query,
        last_intent=payload.last_intent,
    )
    return IntentResponse(**result)