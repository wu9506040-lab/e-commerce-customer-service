"""RefundFlow - 退款业务流（M14 §10 阶段 3）

包装 refund_graph V3（LangGraph 6 节点）：
  fetch_order → judge → fetch_policy → check_proof → escalate / synthesize

设计要点：
- 不替换 V3（D8 决策：Factory → RefundFlow → V3）
- yield meta.flow_stage 让前端展示阶段指示器（"正在审核 → 召回政策 → 生成回复"）
- LangGraph 异常 → fallback 到 V2（保留 handle_refund_v2 保险丝）
- yield 顺序与 handle_refund_v3 保持完全一致（向后兼容）
"""
from __future__ import annotations

import logging
from typing import Any, Generator, Optional, Tuple

from app.services.chat.refund_handler import handle_refund_v2
from app.services.chat.prompt_assembler import NO_LOGIN_PROMPT, _extract_order_no_from_history
from app.services.chat.stream_dispatcher import stream_simple
from app.services.refund_graph import refund_graph_app
from app.services.session_service import ANONYMOUS_USER_ID

logger = logging.getLogger(__name__)


class RefundFlow:
    """退款业务流：包装 LangGraph V3 + 显式 stage 推送

    阶段名（与 LangGraph node_name 对齐）：
    - fetch_order    → 查订单
    - judge          → 规则判断可退性
    - fetch_policy   → 召回政策条款
    - check_proof    → 检查用户凭证
    - escalate       → 升级人工
    - synthesize     → LLM 综合答案
    """

    name = "refund"

    def __init__(
        self,
        query: str,
        user_id: int,
        intent_result: dict,
        order_no: Optional[str] = None,
        context_block: str = "",
        history: Optional[list[dict]] = None,
    ) -> None:
        self.query = query
        self.user_id = user_id
        self.intent_result = intent_result
        self.order_no = order_no
        self.context_block = context_block
        self.history = history

    def run(self) -> Generator[Tuple[str, Any], None, None]:
        """执行 RefundFlow，按节点顺序 yield SSE 事件

        yield 内容：
        - ("meta", {...})：每个阶段都先 yield meta（含 flow_stage 字段）
        - ("token", str)：LLM 综合答案（synthesize 阶段）
        - ("done", {"answer": str})：流结束（langgraph 路径需手动 yield，refund_handler 同款）
        """
        entities = self.intent_result["entities"]
        # 与 handle_refund_v3 逻辑完全一致：context > entities > history 兜底
        effective_order_no = (
            self.order_no
            or entities.get("order_no")
            or _extract_order_no_from_history(self.history)
        )

        # 1. 鉴权：匿名用户 → 短路
        if self.user_id == ANONYMOUS_USER_ID:
            yield ("meta", {
                "intent": "refund_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
                "flow_stage": "fetch_order",
                "v3_engine": "langgraph",
            })
            yield from stream_simple(NO_LOGIN_PROMPT)
            return

        # 2. 无 order_no：直接请用户提供（不允许自动 fallback 到最近订单 — M9.5 防串单）
        if not effective_order_no:
            yield ("meta", {
                "intent": "refund_query",
                "entities": entities,
                "contexts": [],
                "scores": [],
                "flow_stage": "fetch_order",
                "v3_engine": "langgraph",
            })
            yield from stream_simple(
                "请提供要查询退款的订单号（格式示例：ORD20260628004）。"
            )
            return

        # 3. 调 LangGraph refund_graph_app.stream() 边执行边输出
        # 与 handle_refund_v3 的关键差异：每个节点都 yield meta + flow_stage
        # 前端可订阅 flow_stage 实现阶段指示器（如 "正在查订单 → 正在审核 → 召回政策 → 生成回复"）
        meta_emitted = False
        try:
            for event in refund_graph_app.stream(
                {
                    "user_id": self.user_id,
                    "order_no": effective_order_no,
                    "query": self.query,
                    "context_block": self.context_block,
                    "history": self.history or [],
                },
                stream_mode="updates",
            ):
                for node_name, state_update in event.items():
                    if node_name.startswith("__"):
                        continue

                    if node_name == "judge":
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": "judge",
                            "v3_engine": "langgraph",
                            "refundable": state_update.get("refundable"),
                            "reason": state_update.get("reason"),
                            "days_since_order": state_update.get("days_since_order"),
                        })
                        meta_emitted = True
                    elif node_name == "fetch_policy":
                        # 召回阶段：meta 推送（带 policy_hits），让前端能感知"正在查条款"
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": "fetch_policy",
                            "v3_engine": "langgraph",
                            "policy_hits": len(state_update.get("policy_docs", [])),
                        })
                    elif node_name == "check_proof":
                        # 凭证检查阶段：meta 推送（带 escalate_to_human）
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": "check_proof",
                            "v3_engine": "langgraph",
                            "escalate_to_human": state_update.get("escalate_to_human", False),
                        })
                    elif node_name in ("synthesize", "escalate"):
                        if not meta_emitted:
                            # 兜底：judge 一定先于 synthesize
                            yield ("meta", {
                                "intent": "refund_query",
                                "entities": entities,
                                "contexts": [],
                                "scores": [],
                                "order_no": effective_order_no,
                                "flow_stage": node_name,
                                "v3_engine": "langgraph",
                            })
                            meta_emitted = True
                        # 终止阶段：yield meta（带 stage）+ token（final_answer）
                        yield ("meta", {
                            "intent": "refund_query",
                            "entities": entities,
                            "contexts": [],
                            "scores": [],
                            "order_no": effective_order_no,
                            "flow_stage": node_name,
                            "v3_engine": "langgraph",
                        })
                        chunk = state_update.get("final_answer", "")
                        if chunk:
                            yield ("token", chunk)

            # 修复：langgraph 路径需手动 yield done（与 handle_refund_v3 一致）
            yield ("done", {"answer": ""})

        except Exception as e:
            # LangGraph 挂了 → fallback 到 V2（保险丝）
            logger.exception(
                f"RefundFlow LangGraph 执行失败，fallback 到 V2: "
                f"order={effective_order_no} err={e}"
            )
            yield from handle_refund_v2(
                self.query,
                self.user_id,
                self.intent_result,
                order_no=self.order_no,
                context_block=self.context_block,
            )