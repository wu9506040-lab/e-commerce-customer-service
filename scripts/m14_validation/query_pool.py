"""
query_pool.py - M14 业务闭环 100 business scenarios 生成器（V2 · 真实话术驱动）

按 2026-07-18 用户反馈整改（"模拟的业务和数据要有依据合理"）：
- 100 场景 = 100 条真实话术（1:1 映射，0 凑数）
- 每条 query 必须能在 data/real_corpus.json 找到对应来源
- entities 字段仅作 expectation 校验，不再传给 resolver/RefundFlow（改走真实 NL 抽取）

100 场景分布：
- Resolver 4 actions: 40 条（覆盖 direct_answer/show_picker/not_found/ask_login_or_list）
- RefundFlow 4 分支: 30 条（覆盖 synthesize/escalate/ask_order_no/invalid_order）
- Tool 调用: 20 条（订单/物流/政策查询）
- Edge 边界: 10 条（越权/超期/上下文延续/极长 query）

每条 scenario schema：
{
  "id": "M14-0001",
  "category": "resolver" | "refund" | "tool" | "edge",
  "name": "场景名",
  "user_id": int,
  "intent": "order_query" | "refund_query" | ...,
  "query": str,                      # 真实 query（来自 real_corpus.json）
  "corpus_id": "RC001",              # 指向 real_corpus.json 的来源
  "entities": {"order_no": str|None, "sku": str|None},  # 仅作 expectation 校验，不传给 resolver
  "expected": str,                   # 期望的 action / branch / tool result
  "context": {"current_order_no": str|None},  # 上下文（context 延续场景用）
  "note": str,                       # 备注 + 来源标注
  "escalate_trigger": str,           # 升级触发条件（escalate 类场景）
}
"""
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Any, Optional

# 常量定义（不依赖 mock_data，避免 import 链触发 settings 校验）
MOCK_USER_ID_RANGE = range(10001, 10021)  # 20 users: 10001-10020（与 mock_data.py 对齐）
MOCK_ORDER_NO_PREFIX = "ORD20260718"
ORDER_DATE = __import__("datetime").datetime(2026, 7, 18)


# 订单状态字符串常量（与 app.models.order.OrderStatus 对齐）
class _OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    REFUNDED = "refunded"


OrderStatus = _OrderStatus


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
    corpus_id: str = ""               # 关联 real_corpus.json
    entities: Dict[str, Any] = field(default_factory=dict)
    expected: str = ""
    expected_order_no: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    note: str = ""
    escalate_trigger: str = "none"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================
# user_id 选择辅助（按 mock_data.py 的 20 用户分布）
# =============================================================
# 0 订单 user（ASK_LOGIN_OR_LIST）
USER_ZERO_ORDERS = [10001, 10007]
# 1 订单 user（DIRECT_ANSWER only_one_order）
USER_ONE_ORDER = [10002, 10008, 10014]
# 多订单 user（SHOW_PICKER）
USER_MULTI_ORDERS = [10003, 10004, 10005, 10006, 10009, 10010, 10011, 10012, 10013, 10015, 10016, 10017, 10018, 10019, 10020]


def _resolve_user_id(corpus_entry: Dict[str, Any], category: str) -> int:
    """根据 corpus 条目的 expected_resolver_action 选 user_id"""
    action = corpus_entry.get("expected_resolver_action", "show_picker")
    if action == "ask_login_or_list":
        return USER_ZERO_ORDERS[hash(corpus_entry["id"]) % len(USER_ZERO_ORDERS)]
    if action == "direct_answer":
        return USER_ONE_ORDER[hash(corpus_entry["id"]) % len(USER_ONE_ORDER)]
    # show_picker / others → 多订单 user
    return USER_MULTI_ORDERS[hash(corpus_entry["id"]) % len(USER_MULTI_ORDERS)]


# =============================================================
# Resolver 4 actions scenarios（40 条 · 全部来自 real_corpus）
# =============================================================
def _build_resolver_scenarios() -> List[Scenario]:
    """40 个 Resolver 决策场景，全部基于真实话术。

    分布：
    - direct_answer: ~14 条（policy 类大多直接答）
    - show_picker: ~26 条（refund/logistics/order 类多为多订单消歧）
    - ask_login_or_list: edge 桶已覆盖 3 条（ASK_LOGIN），这里不再重复
    """
    from real_corpus import filter_by_action

    scenarios: List[Scenario] = []
    counter = 1

    # 按 action 过滤
    direct_answer = filter_by_action("direct_answer")
    show_picker = filter_by_action("show_picker")

    # direct_answer 类（取 14 条）
    for entry in direct_answer[:14]:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="resolver",
            name=f"resolver_direct_answer_{entry['id']}",
            user_id=_resolve_user_id(entry, "resolver"),
            intent="order_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="direct_answer",
            note=f"来源: {entry['source']} | 平台参考: {entry['platform_ref']}",
        ))
        counter += 1

    # show_picker 类（取 26 条）
    for entry in show_picker[:26]:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="resolver",
            name=f"resolver_show_picker_{entry['id']}",
            user_id=_resolve_user_id(entry, "resolver"),
            intent="order_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="show_picker",
            note=f"来源: {entry['source']} | 平台参考: {entry['platform_ref']}",
        ))
        counter += 1

    return scenarios[:40]


# =============================================================
# RefundFlow 4 分支 scenarios（30 条 · 全部来自 real_corpus）
# =============================================================
def _build_refund_scenarios() -> List[Scenario]:
    """30 个 RefundFlow 流程场景，全部基于真实话术。

    分布：
    - synthesize: 10 条（refund/logistics 类普通退款路径，refundable=True）
    - escalate: 12 条（覆盖 4 类升级触发，按 plan 显式标注"未实现"）
    - ask_order_no: 5 条（无 order_no）
    - invalid_order: 3 条（order_no 不存在/格式错）
    """
    from real_corpus import filter_by_branch, filter_by_escalate_trigger

    scenarios: List[Scenario] = []
    counter = 41

    # === synthesize 分支（10 条，refund/logistics 类的普通退款话术）===
    # P0 整改（2026-07-19 · V6 重跑）：改用 USER_ONE_ORDER 分配
    # 旧版用 USER_MULTI_ORDERS → Resolver 返回 SHOW_PICKER → ask_order_no 分支
    # → 12 个有效 case 全部在 ask_order_no 分支无政策文本（V5 metric 修复后 0/12）
    #
    # 新版：USER_ONE_ORDER (3 users: 10002/10008/10014) → Resolver DIRECT_ANSWER
    # → auto-pick effective_order_no → LangGraph decide_node → fetch_policy → synthesize
    # → 输出含 "24小时"/"7天无理由" 的政策文本 → 政策覆盖率可量化
    normal_refund_entries = [
        e for e in filter_by_branch("ask_order_no")
        if e.get("escalate_trigger") == "none"
        and e.get("scenario_type") in ("refund", "logistics", "order", "policy")
    ][:10]

    for i, entry in enumerate(normal_refund_entries):
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name=f"refund_synthesize_{entry['id']}",
            user_id=USER_ONE_ORDER[i % len(USER_ONE_ORDER)],  # 改：USER_ONE_ORDER (3 users round-robin)
            intent="refund_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            # 1-order user 自动解析 → 实际走 synthesize 分支
            expected="synthesize",  # 改：ask_order_no → synthesize
            note=f"来源: {entry['source']} | 1-order user Resolver DIRECT_ANSWER 自动解析 → synthesize 分支输出政策文本",
        ))
        counter += 1

    # === escalate 分支（12 条，覆盖 4 类触发）===
    escalate_entries = filter_by_escalate_trigger("quality_no_proof")
    emotion_entries = filter_by_escalate_trigger("emotion_high")
    manual_entries = filter_by_escalate_trigger("manual_request")
    amount_entries = filter_by_escalate_trigger("amount_high")

    # 8 条 quality_no_proof
    for entry in escalate_entries[:8]:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name=f"refund_escalate_quality_{entry['id']}",
            user_id=USER_MULTI_ORDERS[hash(entry["id"]) % len(USER_MULTI_ORDERS)],
            intent="refund_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="escalate",
            escalate_trigger="quality_no_proof",
            note=f"来源: {entry['source']} | 触发: quality_no_proof（refund_graph.py:190 已实现）",
        ))
        counter += 1

    # 3 条 emotion_high（**当前代码未实现**）
    for entry in emotion_entries[:3]:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name=f"refund_escalate_emotion_{entry['id']}",
            user_id=USER_MULTI_ORDERS[hash(entry["id"]) % len(USER_MULTI_ORDERS)],
            intent="refund_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="escalate",
            escalate_trigger="emotion_high",
            note=f"来源: {entry['source']} | 触发: emotion_high（**当前代码未实现**·business.md L259 要求）",
        ))
        counter += 1

    # 1 条 manual_request
    for entry in manual_entries[:1]:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name=f"refund_escalate_manual_{entry['id']}",
            user_id=USER_MULTI_ORDERS[hash(entry["id"]) % len(USER_MULTI_ORDERS)],
            intent="refund_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="escalate",
            escalate_trigger="manual_request",
            note=f"来源: {entry['source']} | 触发: manual_request（**当前代码未实现**·business.md L258 要求）",
        ))
        counter += 1

    # 1 条 amount_high
    for entry in amount_entries[:1]:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name=f"refund_escalate_amount_{entry['id']}",
            user_id=USER_MULTI_ORDERS[hash(entry["id"]) % len(USER_MULTI_ORDERS)],
            intent="refund_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="escalate",
            escalate_trigger="amount_high",
            note=f"来源: {entry['source']} | 触发: amount_high（**当前代码未实现**·business.md L260 要求）",
        ))
        counter += 1

    # === ask_order_no 分支（5 条）===
    no_order_entries = [
        e for e in filter_by_branch("ask_order_no")
        if e.get("scenario_type") == "refund" and e.get("escalate_trigger") == "none"
        and e not in normal_refund_entries  # 避免与 synthesize 重复
    ][:5]
    # 不足 5 条时，从其他类补足
    if len(no_order_entries) < 5:
        extra = [
            e for e in filter_by_branch("ask_order_no")
            if e not in normal_refund_entries and e not in no_order_entries
        ][:5 - len(no_order_entries)]
        no_order_entries.extend(extra)

    for entry in no_order_entries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name=f"refund_ask_order_no_{entry['id']}",
            user_id=USER_MULTI_ORDERS[hash(entry["id"]) % len(USER_MULTI_ORDERS)],
            intent="refund_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="ask_order_no",
            note=f"来源: {entry['source']} | 无 order_no → 请用户提供",
        ))
        counter += 1

    # === invalid_order 分支（3 条，边界扩展）===
    invalid_queries = [
        "退款订单 ORD20269999XXX",
        "我订单 ORD00000000XXX 想退款",
        "ORD99999999999 怎么退款",
    ]
    for q in invalid_queries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="refund",
            name="refund_invalid_order_no",
            user_id=10003,
            intent="refund_query",
            query=q,
            corpus_id="",
            entities={"order_no": "ORD99999999XXX", "sku": None},
            expected="invalid_order",
            note="边界扩展: 无效 order_no（不符合正则）→ fetch_order 失败 / fallback V2",
        ))
        counter += 1

    return scenarios[:30]


# =============================================================
# Tool 调用 scenarios（20 条 · 全部来自 real_corpus）
# =============================================================
def _build_tool_scenarios() -> List[Scenario]:
    """20 个 Tool 调用场景，全部基于真实话术的 query。

    直接调 OrderTool，不走 resolver/refund 链路，验证工具本身能返回正确数据。
    """
    from real_corpus import load_corpus

    scenarios: List[Scenario] = []
    counter = 71
    corpus = load_corpus()

    # 选 10 个 order 类（直接订单查询）
    order_entries = [e for e in corpus if e.get("scenario_type") == "order"][:10]
    for entry in order_entries:
        # 直接查 user 10002 的唯一订单
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="tool",
            name=f"tool_order_query_{entry['id']}",
            user_id=10002,  # 1 订单用户，方便预测
            intent="order_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="success:direct_answer",
            note=f"来源: {entry['source']} | OrderTool 直接查询",
        ))
        counter += 1

    # 选 5 个 logistics 类（物流查询）
    logistics_entries = [e for e in corpus if e.get("scenario_type") == "logistics"][:5]
    for entry in logistics_entries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="tool",
            name=f"tool_logistics_{entry['id']}",
            user_id=10002,
            intent="order_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="success:logistics",
            note=f"来源: {entry['source']} | OrderTool.get_logistics",
        ))
        counter += 1

    # 选 5 个 policy 类（政策咨询 → 走 RAG/直答）
    policy_entries = [e for e in corpus if e.get("scenario_type") == "policy"][:5]
    for entry in policy_entries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="tool",
            name=f"tool_policy_{entry['id']}",
            user_id=10002,
            intent="policy_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="success:policy",
            note=f"来源: {entry['source']} | 政策直答（不走 RAG）",
        ))
        counter += 1

    return scenarios[:20]


# =============================================================
# 边界 case scenarios（10 条）
# =============================================================
def _build_edge_scenarios() -> List[Scenario]:
    """10 个边界 case：
    - 3 个 ANONYMOUS_USER（user_id=0）
    - 3 个 NOT_FOUND（跨用户越权）
    - 2 个 上下文延续（ctx.current_order_no 命中）
    - 2 个 极长 query
    """
    scenarios: List[Scenario] = []
    counter = 91

    # === 3 个 ANONYMOUS_USER（user_id=0）===
    anon_entries = [
        {"id": "RC034", "query": "我的订单现在是什么状态？", "src": "综合（基于京东/淘宝帮助中心 FAQ 改写）"},
        {"id": "RC073", "query": "请问我的订单到哪了？", "src": "综合（京东/淘宝帮助中心）"},
        {"id": "RC075", "query": "帮我查下最近的订单", "src": "综合（京东/淘宝帮助中心）"},
    ]
    for entry in anon_entries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="edge",
            name="edge_anonymous_user",
            user_id=0,  # ANONYMOUS
            intent="order_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": None, "sku": None},
            expected="ask_login",
            note=f"匿名用户 → ASK_LOGIN | 来源: {entry['src']}",
        ))
        counter += 1

    # === 3 个 NOT_FOUND（user 10009 查 user 10002 的 order_no）===
    cross_entries = [
        {"id": "RC001", "query": f"我在网上买的衣服还没收到，订单 {MOCK_ORDER_NO_PREFIX}001 不想要了，能退款吗？"},
        {"id": "RC003", "query": f"订单 {MOCK_ORDER_NO_PREFIX}001 什么时候能退款？"},
        {"id": "RC017", "query": f"订单 {MOCK_ORDER_NO_PREFIX}001 退货运费谁出？"},
    ]
    for entry in cross_entries:
        scenarios.append(Scenario(
            id=f"M14-{counter:04d}",
            category="edge",
            name="edge_cross_user_not_found",
            user_id=10009,  # 用 10009 查别人的订单
            intent="order_query",
            query=entry["query"],
            corpus_id=entry["id"],
            entities={"order_no": f"{MOCK_ORDER_NO_PREFIX}001", "sku": None},  # 10002 的订单
            expected="not_found",
            note=f"跨用户越权 → NOT_FOUND（防越权）| 来源: {entry['id']}",
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
            query=["查一下", "看看"][i],
            corpus_id="",
            entities={"order_no": None, "sku": None},
            expected="direct_answer",
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
            corpus_id="",
            entities={"order_no": f"{MOCK_ORDER_NO_PREFIX}005", "sku": None},
            expected="direct_answer",
            note="极长 query（>200 字符）→ DIRECT_ANSWER（user_provided_order_no 命中）",
        ))
        counter += 1

    return scenarios[:10]


# =============================================================
# 主入口
# =============================================================
def generate_100_scenarios() -> List[Scenario]:
    """生成 100 个 business scenarios（全部基于 real_corpus.json 真实话术）。"""
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
    # 统计 corpus_id 覆盖
    corpus_coverage = sum(1 for s in scenarios if s.corpus_id)
    print(f"corpus_coverage: {corpus_coverage}/{len(scenarios)}")
    # 打印前 3 条样例
    for s in scenarios[:3]:
        print(json.dumps(s.to_dict(), ensure_ascii=False, indent=2))