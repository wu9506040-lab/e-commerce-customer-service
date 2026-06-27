"""
退款流程 - LangGraph 版（V3）

vs 原 refund_service.check_refundable_with_policy 的区别：
- 原版：固定 3 步（查订单 → 算天数 → 查政策），if/else 嵌套
- LangGraph 版：动态 5-6 步 + 显式状态图 + 条件分支 + 可观测

StateGraph 流程：
  fetch_order → judge → (条件:可退?) → fetch_policy → (条件:可退?) → check_proof
                                                              → (条件:需凭证?) → escalate / synthesize

使用：
    from app.services.refund_graph import refund_graph_app
    result = refund_graph_app.invoke({
        "user_id": 42,
        "order_no": "ORD001",
        "query": "我三天前买的耳机能退吗",
    })
    print(result["final_answer"])
"""
import datetime
from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.core.qwen import chat as qwen_chat
from app.services.policy_service import PolicyService
from app.tools.order_tool import OrderTool


# =============================================================
# State 定义（TypedDict：每个字段就是图的一个"槽位"）
# =============================================================
class RefundState(TypedDict, total=False):
    # 输入
    user_id: int
    order_no: str
    query: str
    user_proof: dict          # 可选：用户上传的凭证

    # Node 1 输出
    order_info: dict          # OrderTool 返回的订单 dict

    # Node 2 输出
    days_since_order: int
    refundable: bool
    reason: str

    # Node 3 输出
    policy_docs: list         # PolicyService 召回的条款

    # Node 4 输出
    escalate_to_human: bool

    # 最终输出（Node 5 或 Node 6 之一填）
    final_answer: str


# =============================================================
# Node 函数 — 6 个
# =============================================================
def fetch_order(state: RefundState) -> RefundState:
    """
    Node 1: 查订单（Tool 调用）

    输入: state["user_id"], state["order_no"]
    输出（写回 state）: order_info (dict), days_since_order (int)
    """
    order = OrderTool.get_order_by_no(state["user_id"], state["order_no"])

    if order is None:
        # 订单不存在 → 兜底值，让后续 judge 返回"订单不存在"
        return {"order_info": {}, "days_since_order": 999}

    # 算天数（create_time 是 ISO 字符串）
    create_time = datetime.datetime.fromisoformat(order["create_time"])
    days = (datetime.datetime.now() - create_time).days

    return {"order_info": order, "days_since_order": days}


def judge_basic_refundable(state: RefundState) -> RefundState:
    """
    Node 2: 基础规则判断（纯业务逻辑，不调 LLM）

    4 种 case 按顺序判断：
        - 订单不存在         → refundable=False, reason="订单不存在"
        - 订单已退款         → refundable=False, reason="该订单已退款"
        - 已签收 + 7 天内    → refundable=True,  reason="符合 7 天无理由"
        - 其他情况           → refundable=False, reason="超过 7 天或未签收"

    注意：return 里 pass-through days_since_order（LangGraph stream_mode=updates
    只返回本 node 的 delta，前置 fetch_order 写入的字段需要显式 pass-through
    才能在 stream event 里被下游看到）。
    """
    order = state.get("order_info", {})
    days = state.get("days_since_order", 999)

    if not order:
        return {"refundable": False, "reason": "订单不存在", "days_since_order": days}
    if order.get("status") == "refunded":
        return {"refundable": False, "reason": "该订单已退款", "days_since_order": days}
    if order.get("status") == "delivered" and days <= 7:
        return {"refundable": True, "reason": "符合 7 天无理由", "days_since_order": days}
    return {"refundable": False, "reason": "超过 7 天或未签收", "days_since_order": days}


def fetch_policy(state: RefundState) -> RefundState:
    """
    Node 3: 召回相关政策（Policy Service RAG）

    仅在 refundable=True 时执行（由 should_fetch_policy 控制）
    """
    docs = PolicyService.search_policy(state["query"], top_k=3)
    return {"policy_docs": docs}


def check_user_proof(state: RefundState) -> RefundState:
    """
    Node 4: 检查用户凭证（质量问题场景需要凭证）

    规则：
        - query 含 "质量" 且 user_proof 为空 → escalate_to_human=True
        - 否则 → escalate_to_human=False
    """
    query = state.get("query", "")
    proof = state.get("user_proof", {})

    if "质量" in query and not proof:
        return {"escalate_to_human": True, "reason": "需提供质量问题凭证"}

    return {"escalate_to_human": False}


def escalate_to_human(state: RefundState) -> RefundState:
    """
    Node 5: 升级人工客服（不走 LLM，固定文本）

    输入: state["reason"]
    输出: final_answer (str)
    """
    return {
        "final_answer": (
            f"您的情况（{state.get('reason', '')}）需要人工客服协助，"
            "已为您转接，请稍候..."
        )
    }


def synthesize_answer(state: RefundState) -> RefundState:
    """
    Node 6: LLM 综合答案（走 Qwen 生成最终回答）

    Prompt 优先级硬约束（参考 synthesizer.py:51-73）：
        【事实陈述】(最高优先级) → 【政策依据】→ 问题
    """
    # 1. 拼 policy 摘录（每条前 200 字，[1]/[2]/[3] 编号）
    policy_lines = []
    for i, doc in enumerate(state.get("policy_docs", [])[:3], 1):
        text = (doc.get("text", "") or "")[:200]
        if text:
            policy_lines.append(f"[{i}] {text}")
    policy_block = "\n".join(policy_lines) if policy_lines else "（无相关政策）"

    # 2. 拼 prompt
    prompt = (
        "你是专业的电商客服，请严格基于以下【事实陈述】和【政策依据】回答用户。\n\n"
        "【事实陈述】(最高优先级)\n"
        f"订单: {state.get('order_info', {})}\n"
        f"可否退款: {state.get('refundable', False)}\n"
        f"原因: {state.get('reason', '')}\n"
        f"已下单 {state.get('days_since_order', 0)} 天\n\n"
        "【政策依据】\n"
        f"{policy_block}\n\n"
        f"问题: {state.get('query', '')}\n\n"
        "请简洁回答，必要时引用订单号、价格、政策条款编号。"
    )

    # 3. 调 LLM
    result = qwen_chat(
        [{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    return {"final_answer": result["reply"]}


# =============================================================
# 条件分支函数
# =============================================================
def should_fetch_policy(state: RefundState) -> str:
    """可退才查政策，否则直接生成答案"""
    return "fetch_policy" if state.get("refundable") else "synthesize"


def should_check_proof(state: RefundState) -> str:
    """可退才检查凭证，否则直接生成答案"""
    return "check_proof" if state.get("refundable") else "synthesize"


def should_escalate(state: RefundState) -> str:
    """需要凭证才升级人工，否则直接生成答案"""
    return "escalate_to_human" if state.get("escalate_to_human") else "synthesize"


# =============================================================
# 构建图
# =============================================================
def build_refund_graph():
    workflow = StateGraph(RefundState)

    # 注册 Node
    workflow.add_node("fetch_order", fetch_order)
    workflow.add_node("judge", judge_basic_refundable)
    workflow.add_node("fetch_policy", fetch_policy)
    workflow.add_node("check_proof", check_user_proof)
    workflow.add_node("escalate", escalate_to_human)
    workflow.add_node("synthesize", synthesize_answer)

    # 入口
    workflow.set_entry_point("fetch_order")

    # 固定边
    workflow.add_edge("fetch_order", "judge")

    # 条件边
    workflow.add_conditional_edges(
        "judge", should_fetch_policy,
        {"fetch_policy": "fetch_policy", "synthesize": "synthesize"},
    )
    workflow.add_conditional_edges(
        "fetch_policy", should_check_proof,
        {"check_proof": "check_proof", "synthesize": "synthesize"},
    )
    workflow.add_conditional_edges(
        "check_proof", should_escalate,
        {"escalate_to_human": "escalate", "synthesize": "synthesize"},
    )

    # 终止
    workflow.add_edge("synthesize", END)
    workflow.add_edge("escalate", END)

    return workflow.compile()


# 单例（应用启动时编译一次）
refund_graph_app = build_refund_graph()