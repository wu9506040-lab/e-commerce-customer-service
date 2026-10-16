"""
XianyuRefundOrchestratorBridge — V16.8 P0-A

把 XianyuRefundHandler（基于 DB 只读 + 转人工决策）适配成
orchestrator.run_stream yield 的 SSE 事件序列，让主入口的 channel 判断可以
把闲鱼渠道的 refund_query 路由到这里，而不是商城的 chat.refund_handler。

设计原则：
- 智能客服不操作退款（只读 XianyuOrderRepository + 转人工/拒绝）
- 不调 LLM（沿用 V16.3「退款决策由规则层决定」原则）
- 临时建 DB session（与 chat.py::_xianyu_inject_context 现有方式一致，YAGNI 不引入 db lifecycle 重构）

调用方：
  orchestrator.Synthesizer.run_stream 当 primary=refund_query 且 channel=xianyu 时
"""
from __future__ import annotations

import logging
import os
from typing import Any, Generator, Tuple

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.runtime_log import log_event  # V16.5 全链路日志
from app.services.xianyu.refund_handler import XianyuRefundHandler

logger = logging.getLogger(__name__)


def _resolve_seller_unb(seller_unb: str | None) -> str:
    """闲鱼卖家 unb 解析：优先参数，回退环境变量。

    Args:
        seller_unb: 直接传入（测试或特殊场景显式覆盖）

    Returns:
        non-empty seller_unb；环境变量也缺失时返回占位 "default-seller" 兜底
        （handler 查不到记录会落到「无订单记录 → 转人工」分支，不会崩）
    """
    if seller_unb:
        return seller_unb
    env = os.environ.get("XIANYU_SELLER_UNB")
    if env:
        return env
    return "default-seller"


def handle_xianyu_refund(
    query: str,
    buyer_unb: str,
    seller_unb: str | None = None,
    intent_result: dict | None = None,
    db_session: Any | None = None,  # 测试注入；None 时内部自建（与 daemon _xianyu_inject_context 同模式）
) -> Generator[Tuple[str, Any], None, None]:
    """闲鱼退款场景 SSE 兼容 yield

    步骤：
    1. yield meta.intent=refund_query + xianyu_refund 元数据
    2. XianyuRefundHandler.handle_refund_request 查 DB 出 RefundDecision
    3. yield token（reply_text 按 10 字 chunk 切分，模拟正常流式输出）
    4. yield done with answer + order_state + escalation_priority

    Args:
        query: 用户问题（仅日志用）
        buyer_unb: 闲鱼买家 unb（必填，handler 查 DB 用）
        seller_unb: 闲鱼卖家 unb（可选；None 时从 XIANYU_SELLER_UNB env 读）
        intent_result: orchestrator 透传（intent + entities + method）
    """
    intent_result = intent_result or {}
    seller_unb_resolved = _resolve_seller_unb(seller_unb)

    # V16.5 全链路日志：HANDLE_REFUND 进入
    log_event(
        "HANDLE_REFUND", "INFO", "-", buyer_unb or "-",
        f"XIANYU ENTER query={query[:80]} buyer_unb={buyer_unb} "
        f"seller_unb={seller_unb_resolved}",
        version="xianyu", query=query[:200],
        buyer_unb=buyer_unb, seller_unb=seller_unb_resolved,
    )

    # 与 daemon 启动（scripts/run_xianyu_daemon.py）一致：env 优先于 settings 缓存
    # 这样测试 fixture 可以临时把 DATABASE_URL 指向 SQLite，settings 已缓存 MySQL URL 也不影响
    if db_session is not None:
        db = db_session
        own_db = False
    else:
        db_url = os.environ.get("DATABASE_URL") or settings.DATABASE_URL
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)
        db = Session()
        own_db = True
    try:
        handler = XianyuRefundHandler(db=db, seller_unb=seller_unb_resolved)
        decision = handler.handle_refund_request(unb=buyer_unb, buyer_nick="")

        # V16.5 全链路日志：决策结果
        log_event(
            "HANDLE_REFUND", "INFO", "-", buyer_unb or "-",
            f"XIANYU DECISION order_state={decision.order_state} "
            f"escalate={decision.should_escalate} reject={decision.should_reject}",
            version="xianyu",
            order_state=decision.order_state,
            escalate=decision.should_escalate,
            reject=decision.should_reject,
            order_no=decision.order_no,
        )

        # 与商城 refund v3 对齐：meta 里塞 channel + 决策元数据
        meta_extra = {
            "order_state": decision.order_state,
            "order_no": decision.order_no,
            "should_reject": decision.should_reject,
            "should_escalate": decision.should_escalate,
            "escalation_priority": decision.escalation_priority,
            "reject_reason": decision.reject_reason,  # V16.8 P0-A：暴露给前端做诊断/埋点
        }

        # === 1. meta 事件 ===
        yield ("meta", {
            "intent": "refund_query",
            "entities": intent_result.get("entities", {}),
            "contexts": [],
            "scores": [],
            "channel": "xianyu",
            "xianyu_refund": meta_extra,
        })

        # === 2. token 事件（reply_text 按 chunk 切，模拟正常 SSE 流） ===
        reply_text = decision.reply_text or ""
        chunk_size = 10
        for i in range(0, len(reply_text), chunk_size):
            yield ("token", reply_text[i:i + chunk_size])

        # === 3. done 事件（含 xianyu_refund 元数据供前端解析，避免扫 token） ===
        yield ("done", {
            "answer": reply_text,
            "channel": "xianyu",
            "xianyu_refund": meta_extra,
        })
    finally:
        if own_db:
            db.close()
            engine.dispose()


__all__ = ["handle_xianyu_refund"]
