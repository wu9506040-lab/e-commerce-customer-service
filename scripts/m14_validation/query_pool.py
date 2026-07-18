"""
query_pool.py - M14 业务闭环 100 business scenarios 生成器

按用户需求：
- 100 scenarios
- 覆盖 Resolver 4 actions + RefundFlow 4 分支 + Tool 调用 + 边界 case

4 核心指标对应：
1. 主动查询覆盖率       → Resolver 4 actions 决策
2. 业务流程完成率       → RefundFlow 4 分支
3. Tool 调用成功率      → OrderTool / RefundTool
4. Hallucination Free Rate → 异常处理 + 答案合理性

每条 scenario 结构：
{
  "id": "M14-0001",
  "category": "resolver" | "refund" | "tool" | "edge",
  "name": "场景名",
  "user_id": int,
  "intent": "order_query" | "refund_query" | ...,
  "query": str,                      # 模拟用户 query
  "entities": {"order_no": str|None, "sku": str|None},
  "expected": str,                   # 期望的 action / branch / tool result
  "context": {"current_order_no": str|None},  # 上下文（context 延续场景用）
  "note": str,                       # 备注
}
"""
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Any, Optional

# 常量定义（不依赖 mock_data，避免 import 链触发 settings 校验）
MOCK_USER_ID_RANGE = range(10001, 10011)  # 10 users: 10001-10010
MOCK_ORDER_NO_PREFIX = "ORD20260718"
ORDER_DATE = __import__("datetime").datetime(2026, 7, 18)


# 订单状态字符串常量（与 app.models.order.OrderStatus 对齐，避免 query_pool 依赖 app.models）
class _OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    REFUNDED = "refunded"


# 暴露为 OrderStatus（与 OrderStatus.SHIPPED.value 兼容）
OrderStatus = _OrderStatus


def _make_order_summary(idx: int, status: str, days_ago: int, amount: float):
    """生成单个订单摘要（轻量版，与 mock_data.py 同款）。"""
    from dataclasses import dataclass
    import datetime as _dt
    @dataclass
    class _OrderSummary:
        order_no: str
        status: str
        total_amount: float
        create_time: str
        days_since_order: int
    create_dt = ORDER_DATE - _dt.timedelta(days=days_ago)
    return _OrderSummary(
        order_no=f"{MOCK_ORDER_NO_PREFIX}{idx:03d}",
        status=status,
        total_amount=amount,
        create_time=create_dt.isoformat(),
        days_since_order=days_ago,
    )


logger = logging.getLogger(__name__)


# =============================================================
# Scenario 数据结构
# =============================================================
@dataclass
class Scenario:
    id: str
    category: str
    name: str
    user_id: int
    intent: str
    query: str
    entities: Dict[str, Any] = field(default_factory=dict)
    expected: str = ""
    expected_order_no: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================
# Resolver 4 actions scenarios（40 条）
# =============================================================
def _build_resolver_scenarios() -> List[Scenario]:
    """40 个 Resolver 决策场景：
    - 10 个 ASK_LOGIN_OR_LIST（0 订单 user 10001）
    - 10 个 DIRECT_ANSWER only_one_order（1 订单 user 10002）
    - 10 个 SHOW_PICKER disambiguate（2 订单 user 10003）
    - 10 个 SHOW_PICKER max picker（5 订单 user 10006）
    """
    scenarios: List[Scenario] = []
    counter = 1

    # === 10 个 ASK_LOGIN_OR_LIST（user 10001 0 订单）===
    queries_for_zero = [
        "我的订单怎么还没到",
        "查一下我的快递",
        "最近的订单状态",
        "我的订单到哪了",
        "我想看订单",
        "看看我的订单",
        "我有什么订单",
        "我的订单列表",
        "查订单",
        "订单查询",
    ]
    for q in queries_for_zero:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="resolver",
            name="ASK_LOGIN_OR_LIST_0_orders",
            user_id=10001,
            intent="order_query",
            query=q,
            entities={"order_no": None, "sku": None},
            expected="ASK_LOGIN_OR_LIST",
            note="user 0 订单 → ASK_LOGIN_OR_LIST",
        ))
        counter += 1

    # === 10 个 DIRECT_ANSWER only_one_order（user 10002 1 订单）===
    queries_for_one = [
        "我的快递怎么还没到",
        "查一下我的快递",
        "最近的订单",
        "我的订单状态",
        "我想看那个订单",
        "看看我的订单",
        "查订单",
        "订单查询",
        "我的货到哪了",
        "我的订单物流",
    ]
    for q in queries_for_one:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="resolver",
            name="DIRECT_ANSWER_only_one_order",
            user_id=10002,
            intent="order_query",
            query=q,
            entities={"order_no": None, "sku": None},
            expected="DIRECT_ANSWER",
            expected_order_no=f"{MOCK_ORDER_NO_PREFIX}001",
            note="user 1 订单 → DIRECT_ANSWER（自动用唯一订单）",
        ))
        counter += 1

    # === 10 个 SHOW_PICKER disambiguate（user 10003 2 订单）===
    queries_for_two = [
        "我的订单",
        "查订单",
        "最近的订单",
        "我的快递",
        "我有什么订单",
        "看一下订单",
        "订单列表",
        "我要查订单",
        "我的订单到哪了",
        "查所有订单",
    ]
    for q in queries_for_two:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="resolver",
            name="SHOW_PICKER_multi_orders",
            user_id=10003,
            intent="order_query",
            query=q,
            entities={"order_no": None, "sku": None},
            expected="SHOW_PICKER",
            note="user 2 订单 → SHOW_PICKER（歧义消除）",
        ))
        counter += 1

    # === 10 个 SHOW_PICKER max picker（user 10006 5 订单）===
    queries_for_five = [
        "我的订单",
        "查订单",
        "最近的订单",
        "我的快递",
        "我有什么订单",
        "看一下订单",
        "订单列表",
        "我要查订单",
        "我的订单到哪了",
        "查所有订单",
    ]
    for q in queries_for_five:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="resolver",
            name="SHOW_PICKER_5_orders_max",
            user_id=10006,
            intent="order_query",
            query=q,
            entities={"order_no": None, "sku": None},
            expected="SHOW_PICKER",
            note="user 5 订单 = MAX_PICKER_ITEMS → SHOW_PICKER（边界）",
        ))
        counter += 1

    return scenarios  # 40 条


# =============================================================
# RefundFlow 4 分支 scenarios（30 条）
# =============================================================
def _build_refund_scenarios() -> List[Scenario]:
    """30 个 RefundFlow 流程场景：
    - 8 个 synthesize 分支（refundable=True，正常退款）
    - 8 个 escalate 分支（refundable=False or 凭证缺失）
    - 7 个 无 order_no（请用户提供）
    - 7 个 无效 order_no（fetch_order 失败 → fallback V2）
    """
    scenarios: List[Scenario] = []
    counter = 41

    # === 8 个 synthesize 分支（refundable=True：7 天内 delivered）===
    # user 10010 订单 26 (DELIVERED 3 天前)
    for i in range(8):
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name="refund_synthesize_in_7_days",
            user_id=10010,
            intent="refund_query",
            query=f"我想退款，订单 {MOCK_ORDER_NO_PREFIX}026，刚收到 3 天",
            entities={"order_no": f"{MOCK_ORDER_NO_PREFIX}026", "sku": None},
            expected="synthesize",
            note="7 天内 delivered → judge.refundable=True → synthesize",
        ))
        counter += 1

    # === 8 个 escalate 分支（超 7 天 completed）===
    # user 10004 订单 6 (COMPLETED 30 天前)
    for i in range(8):
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name="refund_escalate_over_7_days",
            user_id=10004,
            intent="refund_query",
            query=f"我要退款，订单 {MOCK_ORDER_NO_PREFIX}006，30 天前的订单",
            entities={"order_no": f"{MOCK_ORDER_NO_PREFIX}006", "sku": None},
            expected="escalate",
            note="超 7 天 completed → judge.refundable=False → escalate",
        ))
        counter += 1

    # === 7 个 无 order_no（请用户提供）===
    no_order_queries = [
        "我想退款",
        "怎么退款",
        "我要申请退款",
        "退款流程是什么",
        "可以退款吗",
        "我下的订单能退款吗",
        "我的订单想退款",
    ]
    for q in no_order_queries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name="refund_no_order_no",
            user_id=10002,
            intent="refund_query",
            query=q,
            entities={"order_no": None, "sku": None},
            expected="ask_order_no",
            note="无 order_no → 请用户提供",
        ))
        counter += 1

    # === 7 个 无效 order_no（订单不存在）===
    invalid_queries = [
        f"退款订单 ORD20269999XXX",
        f"我订单 ORD00000000XXX 想退款",
        f"ORD99999999999 退款",
        f"ORD20260101001 怎么退款",
        f"我要退 ORD88888888ABC",
        f"ORD77777777XYZ 退款",
        f"ORD66666666QQQ 申请退款",
    ]
    for q in invalid_queries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name="refund_invalid_order_no",
            user_id=10003,
            intent="refund_query",
            query=q,
            entities={"order_no": "ORD99999999XXX", "sku": None},  # 不符合 ORDER_NO_PATTERN
            expected="invalid_order",
            note="无效 order_no（不符合正则）→ fetch_order 不调 / fallback V2",
        ))
        counter += 1

    return scenarios  # 30 条 (41-70)


# =============================================================
# Tool 调用 scenarios（20 条）
# =============================================================
def _build_tool_scenarios() -> List[Scenario]:
    """20 个 Tool 调用场景：
    - 10 个 OrderTool.get_order_by_no（直接查单）
    - 5 个 OrderTool.list_user_orders
    - 5 个 OrderTool.get_logistics
    """
    scenarios: List[Scenario] = []
    counter = 71

    # === 10 个 get_order_by_no ===
    order_cases = [
        (10002, "001", OrderStatus.SHIPPED.value),
        (10003, "002", OrderStatus.SHIPPED.value),
        (10003, "003", OrderStatus.DELIVERED.value),
        (10004, "004", OrderStatus.PAID.value),
        (10004, "005", OrderStatus.SHIPPED.value),
        (10005, "007", OrderStatus.PENDING.value),
        (10006, "011", OrderStatus.SHIPPED.value),
        (10007, "016", OrderStatus.PAID.value),
        (10008, "021", OrderStatus.SHIPPED.value),
        (10010, "026", OrderStatus.DELIVERED.value),
    ]
    for user_id, order_suffix, status in order_cases:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="tool",
            name="tool_get_order_by_no",
            user_id=user_id,
            intent="order_query",
            query=f"查订单 {MOCK_ORDER_NO_PREFIX}{order_suffix}",
            entities={"order_no": f"{MOCK_ORDER_NO_PREFIX}{order_suffix}", "sku": None},
            expected=f"success:{status}",
            note=f"OrderTool.get_order_by_no → 期望 status={status}",
        ))
        counter += 1

    # === 5 个 list_user_orders ===
    list_cases = [
        (10002, 1),  # 1 订单
        (10003, 2),  # 2 订单
        (10004, 3),  # 3 订单
        (10006, 5),  # 5 订单
        (10007, 5),  # 5 订单
    ]
    for user_id, expected_count in list_cases:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="tool",
            name="tool_list_user_orders",
            user_id=user_id,
            intent="order_query",
            query="我的所有订单",
            entities={"order_no": None, "sku": None},
            expected=f"success:count_{expected_count}",
            note=f"OrderTool.list_user_orders → 期望 {expected_count} 个订单",
        ))
        counter += 1

    # === 5 个 get_logistics ===
    logistics_cases = [
        (10002, "001", "运输中"),     # shipped
        (10003, "003", "已签收"),     # delivered
        (10004, "004", "待发货"),     # paid
        (10005, "007", "待发货"),     # pending
        (10007, "019", "已签收"),     # delivered
    ]
    for user_id, order_suffix, expected_status in logistics_cases:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="tool",
            name="tool_get_logistics",
            user_id=user_id,
            intent="order_query",
            query=f"查物流 {MOCK_ORDER_NO_PREFIX}{order_suffix}",
            entities={"order_no": f"{MOCK_ORDER_NO_PREFIX}{order_suffix}", "sku": None},
            expected=f"success:{expected_status}",
            note=f"OrderTool.get_logistics → 期望 {expected_status}",
        ))
        counter += 1

    return scenarios  # 20 条 (71-90)


# =============================================================
# 边界 case scenarios（10 条）
# =============================================================
def _build_edge_scenarios() -> List[Scenario]:
    """10 个边界 case：
    - 3 个 ANONYMOUS_USER（user_id=0）
    - 3 个 NOT_FOUND（user 提供了别人的 order_no）
    - 2 个 上下文延续（ctx.current_order_no 命中）
    - 2 个 极长 query
    """
    scenarios: List[Scenario] = []
    counter = 91

    # === 3 个 ANONYMOUS_USER ===
    anon_queries = [
        "我的订单",
        "查我的快递",
        "最近的订单",
    ]
    for q in anon_queries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="edge",
            name="edge_anonymous_user",
            user_id=0,  # ANONYMOUS_USER_ID
            intent="order_query",
            query=q,
            entities={"order_no": None, "sku": None},
            expected="ASK_LOGIN",
            note="匿名用户 → ASK_LOGIN（未登录走固定话术）",
        ))
        counter += 1

    # === 3 个 NOT_FOUND（user 10009 查 user 10002 的 order_no）===
    cross_queries = [
        f"查订单 {MOCK_ORDER_NO_PREFIX}001",  # 10002 的订单
        f"我的订单 {MOCK_ORDER_NO_PREFIX}002",  # 10003 的订单
        f"订单 {MOCK_ORDER_NO_PREFIX}016",  # 10007 的订单
    ]
    for q in cross_queries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="edge",
            name="edge_cross_user_not_found",
            user_id=10009,  # 用 10009 查别人的订单
            intent="order_query",
            query=q,
            entities={"order_no": f"{MOCK_ORDER_NO_PREFIX}001", "sku": None},
            expected="NOT_FOUND",
            note="跨用户越权 → NOT_FOUND（防越权）",
        ))
        counter += 1

    # === 2 个 上下文延续（user 10008 ctx.current_order_no 命中）===
    for i in range(2):
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="edge",
            name="edge_context_continuation",
            user_id=10008,
            intent="order_query",
            query="查一下",
            entities={"order_no": None, "sku": None},
            expected="DIRECT_ANSWER",
            expected_order_no=f"{MOCK_ORDER_NO_PREFIX}021",  # ctx 带
            context={"current_order_no": f"{MOCK_ORDER_NO_PREFIX}021"},
            note="ctx.current_order_no 命中 → DIRECT_ANSWER（上下文延续）",
        ))
        counter += 1

    # === 2 个 极长 query（边界）===
    long_queries = [
        "我想查询一下我最近下的那个订单号是 " + MOCK_ORDER_NO_PREFIX + "001 的那个快递现在到哪里了因为我已经等了好几天了希望能尽快帮我看一下具体位置谢谢",
        "请问我的订单 " + MOCK_ORDER_NO_PREFIX + "005 " + ("现在什么状态" * 30),
    ]
    for q in long_queries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="edge",
            name="edge_very_long_query",
            user_id=10004,
            intent="order_query",
            query=q,
            entities={"order_no": f"{MOCK_ORDER_NO_PREFIX}005", "sku": None},
            expected="DIRECT_ANSWER",
            note="极长 query（>200 字符）→ DIRECT_ANSWER（user_provided_order_no 命中）",
        ))
        counter += 1

    return scenarios  # 10 条 (91-100)


# =============================================================
# 主入口
# =============================================================
def generate_100_scenarios() -> List[Scenario]:
    """生成 100 个 business scenarios。"""
    resolver = _build_resolver_scenarios()       # 40 条
    refund = _build_refund_scenarios()          # 30 条
    tool = _build_tool_scenarios()               # 20 条
    edge = _build_edge_scenarios()               # 10 条
    all_scenarios = resolver + refund + tool + edge
    assert len(all_scenarios) == 100, f"期望 100 条，实际 {len(all_scenarios)}"
    logger.info(f"生成 100 scenarios: resolver={len(resolver)} refund={len(refund)} tool={len(tool)} edge={len(edge)}")
    return all_scenarios


# =============================================================
# CLI
# =============================================================
if __name__ == "__main__":
    import json
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    scenarios = generate_100_scenarios()
    print(f"total: {len(scenarios)}")
    # 统计 category 分布
    from collections import Counter
    dist = Counter(s.category for s in scenarios)
    print(f"distribution: {dict(dist)}")
    # 打印前 3 条样例
    for s in scenarios[:3]:
        print(json.dumps(s.to_dict(), ensure_ascii=False, indent=2))
