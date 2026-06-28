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
    history: list             # M9.5+：多轮对话历史 [{"role":..., "content":...}]
    context_block: str        # M9.5：从商品/订单跳转携带的 context（已拼好的字符串）

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

    days_since_order 计算规则：
        - delivered / completed → 按签收日算（7 天无理由窗口起点）
        - 其他状态               → 按下单日算
        - 签收日由 OrderTool 硬编码 create_time + 2 days 推算
          （Order 表当前无 delivery_time 字段，与 OrderTool.get_logistics 保持一致）
    """
    order = OrderTool.get_order_by_no(state["user_id"], state["order_no"])

    if order is None:
        # 订单不存在 → 兜底值；days 用 0 而非 magic number 999
        return {"order_info": {}, "days_since_order": 0}

    # 算天数（create_time 是 ISO 字符串）
    create_time = datetime.datetime.fromisoformat(order["create_time"])
    status = order.get("status")
    if status in ("delivered", "completed"):
        # 已签收：按签收日算（与 OrderTool.get_logistics 的 create_time + 2 days 一致）
        delivery_time = create_time + datetime.timedelta(days=2)
        days = (datetime.datetime.now() - delivery_time).days
    else:
        # 其他状态：按下单日算
        days = (datetime.datetime.now() - create_time).days

    return {"order_info": order, "days_since_order": days}


# 7 天无理由窗口常量（与 RefundTool.REFUND_WINDOW_DAYS 保持一致）
REFUND_WINDOW_DAYS = 7
# 签收日偏移（与 OrderTool.get_logistics 一致；Order 模型暂无 delivery_time 字段）
DELIVERY_OFFSET_DAYS = 2

# 状态中文映射（用于 prompt 注入和 reason 拼接）
_STATUS_ZH = {
    "pending":   "待支付",
    "paid":      "已支付",
    "shipped":   "运输中",
    "delivered": "已签收",
    "completed": "已完成",
    "refunded":  "已退款",
}


def judge_basic_refundable(state: RefundState) -> RefundState:
    """
    Node 2: 基础规则判断（纯业务逻辑，不调 LLM）

    规则（与 RefundTool.check_refundable 对齐）：
        - 订单不存在         → refundable=False, reason="订单不存在"
        - 订单已退款         → refundable=False, reason="该订单已退款，无法重复申请"
        - 已签收 + 7 天内    → refundable=True,  reason="已签收 N 天，在 7 天无理由退货期限内"
        - 已签收 + 超 7 天   → refundable=False, reason="已签收 N 天，超过 7 天无理由退货期限"
        - 其他状态           → refundable=True,  reason="订单状态「XX」，可发起退款申请"

    注意：return 里 pass-through days_since_order（LangGraph stream_mode=updates
    只返回本 node 的 delta，前置 fetch_order 写入的字段需要显式 pass-through
    才能在 stream event 里被下游看到）。
    """
    order = state.get("order_info", {})
    days = state.get("days_since_order", 0)
    status = order.get("status")

    if not order:
        return {"refundable": False, "reason": "订单不存在", "days_since_order": days}
    if status == "refunded":
        return {
            "refundable": False,
            "reason": "该订单已退款，无法重复申请",
            "days_since_order": days,
        }
    if status == "delivered":
        if days <= REFUND_WINDOW_DAYS:
            return {
                "refundable": True,
                "reason": f"已签收 {days} 天，在 {REFUND_WINDOW_DAYS} 天无理由退货期限内",
                "days_since_order": days,
            }
        return {
            "refundable": False,
            "reason": f"已签收 {days} 天，超过 {REFUND_WINDOW_DAYS} 天无理由退货期限",
            "days_since_order": days,
        }
    # pending / paid / shipped / completed：都可发起退款申请
    status_zh = _STATUS_ZH.get(status, status)
    return {
        "refundable": True,
        "reason": f"订单状态「{status_zh}」，可发起退款申请",
        "days_since_order": days,
    }


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

    Prompt 优先级硬约束 + 反幻觉 5 条铁律（防止 LLM 胡编乱造 / 串单 / 跑题）：
        1. 必须基于【事实陈述】回答，禁止编造订单号/状态/价格/日期
        2. 【事实陈述】与【政策依据】冲突时，以【事实陈述】为准
        3. 信息不足时直接告知用户，禁止推测
        4. 回答中出现的订单号必须与【事实陈述】中的 order_no 完全一致
        5. 用户问的是"能不能退" → 必须正面回答（可以退 / 不能退 + 原因）
           不能只复述订单基本信息或物流信息；不能确定就转人工

    字段注入：order_no 单独提出来强制注入（防止 LLM 从订单 dict 漏看）
    多轮对话：history 注入，LLM 能基于上下文回答"那能退吗"类追问
    """
    # 1. 拼 policy 摘录（每条前 200 字，[1]/[2]/[3] 编号）
    policy_lines = []
    for i, doc in enumerate(state.get("policy_docs", [])[:3], 1):
        text = (doc.get("text", "") or "")[:200]
        if text:
            policy_lines.append(f"[{i}] {text}")
    policy_block = "\n".join(policy_lines) if policy_lines else "（无相关政策）"

    # 2. 拼对话历史（M9.5+：多轮场景必传）
    history = state.get("history") or []
    history_lines = []
    for msg in history[-6:]:  # 只取最近 6 条，避免 prompt 太长
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            history_lines.append(f"用户：{content}")
        elif role == "assistant":
            history_lines.append(f"客服：{content}")
    history_block = "\n".join(history_lines) if history_lines else "（无历史对话）"

    # 3. 提取订单核心事实（避免把整个 dict 灌进 prompt 让 LLM 漏看关键字段）
    order_info = state.get("order_info", {}) or {}
    order_no = order_info.get("order_no") or state.get("order_no") or "未知"
    order_status = _STATUS_ZH.get(order_info.get("status", ""), order_info.get("status", "未知"))
    order_amount = order_info.get("total_amount", "未知")
    refundable = state.get("refundable", False)
    reason = state.get("reason", "")
    days = state.get("days_since_order", 0)
    query = state.get("query", "")
    context_block = state.get("context_block", "").strip()

    # 4. 拼 prompt — 反幻觉 5 条铁律 + 字段显式注入
    context_section = f"\n【上下文】\n{context_block}\n" if context_block else ""
    prompt = (
        "你是专业的电商客服。请严格按以下规则回答：\n\n"
        "【硬约束 - 违反任何一条都视为错误回答】\n"
        "1. 必须基于【事实陈述】回答，不得编造订单号、状态、价格、日期\n"
        "2. 如果【事实陈述】与【政策依据】冲突，以【事实陈述】为准\n"
        "3. 如果【事实陈述】信息不足（如订单不存在），直接告知用户并请其提供订单号，禁止推测\n"
        "4. 回答中出现的订单号必须与【事实陈述】中的 order_no 完全一致，禁止换单\n"
        "5. 用户问【能不能退/能退款吗】时，必须在第一句明确回答【可以退】或【不能退 + 原因】，"
        "禁止只复述订单基本信息；如系统事实明确，按事实回答；如事实不明，请转人工\n\n"
        "【事实陈述】(最高优先级)\n"
        f"订单号: {order_no}\n"
        f"订单状态: {order_status}\n"
        f"订单金额: ¥{order_amount}\n"
        f"可否退款: {'是' if refundable else '否'}\n"
        f"原因: {reason}\n"
        f"已下单 {days} 天\n\n"
        "【政策依据】\n"
        f"{policy_block}\n\n"
        f"{context_section}\n"
        "【对话历史】(供多轮对话参考)\n"
        f"{history_block}\n\n"
        f"用户当前问题: {query}\n\n"
        "回答（先给结论再补充细节，禁止编造）："
    )

    # 5. 调 LLM — temperature=0.3 降低随机性
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