"""tests/test_m14_resolver_metrics.py

M14 Stage 4：OrderContextResolver 决策质量指标单测

覆盖 4 类指标：
1. multi_order_disambiguation_accuracy（=proactive_list_order_accuracy 别名）
   - 公式：SHOW_PICKER 数 / total_orders_many 数
   - 期望 ≥ 95%
2. no_order_no_completion_rate
   - 公式：ASK_LOGIN_OR_LIST 数 / total_orders_zero 数
   - 期望 100%（0 订单必须返 ASK_LOGIN_OR_LIST）
3. card_triggered_when_expected_rate
   - 公式：card_sent / card_expected
   - 期望 ≥ 95%
4. proactive_list_order_accuracy（同 #1）

测试方法：
- 直接调 metrics.inc_resolver_decision() 模拟 orchestrator 决策
- 调 metrics.snapshot() 读取指标
- 断言比率正确
"""
import os
import sys
from pathlib import Path

# 让 `from app.services.metrics import ...` 能跑
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# 测试环境变量（必须在 import settings 前设置）
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")

from app.services.metrics import metrics  # noqa: E402


# =============================================================
# 辅助：每个测试前重置 M14 计数器（其他指标不动）
# =============================================================
def _reset_m14() -> None:
    """重置 M14 Resolver 决策指标（不影响其他指标）"""
    with metrics._lock:
        metrics.resolver_total = 0
        metrics.resolver_by_action = {}
        metrics.resolver_total_orders_zero = 0
        metrics.resolver_total_orders_one = 0
        metrics.resolver_total_orders_many = 0
        metrics.resolver_card_expected = 0
        metrics.resolver_card_sent_when_expected = 0


# =============================================================
# 1. multi_order_disambiguation_accuracy
# =============================================================
class TestMultiOrderDisambiguationAccuracy:
    """N>=2 订单时返 SHOW_PICKER 的比例（plan 阈值 ≥ 95%）"""

    def test_show_picker_perfect(self):
        """N=10 都返 SHOW_PICKER → 100%"""
        _reset_m14()
        for _ in range(10):
            metrics.inc_resolver_decision(
                action="show_picker", total_orders=3,
                card_sent=True, card_expected=True,
            )
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["multi_order_disambiguation_accuracy"] == 1.0
        assert snap["ratios"]["proactive_list_order_accuracy"] == 1.0

    def test_show_picker_partial(self):
        """N=10 中 9 个返 SHOW_PICKER + 1 个返 DIRECT_ANSWER → 90%"""
        _reset_m14()
        for _ in range(9):
            metrics.inc_resolver_decision(
                action="show_picker", total_orders=3,
                card_sent=True, card_expected=True,
            )
        metrics.inc_resolver_decision(
            action="direct_answer", total_orders=3,
            card_sent=False, card_expected=False,
        )
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["multi_order_disambiguation_accuracy"] == 0.9

    def test_no_many_orders_returns_zero(self):
        """total_orders_many=0 → 比率返 0（避免除零）"""
        _reset_m14()
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["multi_order_disambiguation_accuracy"] == 0.0
        assert snap["ratios"]["proactive_list_order_accuracy"] == 0.0


# =============================================================
# 2. no_order_no_completion_rate
# =============================================================
class TestNoOrderNoCompletionRate:
    """0 订单场景返 ASK_LOGIN_OR_LIST 而非误答的比例（plan 阈值 100%）"""

    def test_zero_orders_all_ask_login_or_list(self):
        """5 个 0 订单场景全返 ASK_LOGIN_OR_LIST → 100%"""
        _reset_m14()
        for _ in range(5):
            metrics.inc_resolver_decision(
                action="ask_login_or_list", total_orders=0,
                card_sent=False, card_expected=False,
            )
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["no_order_no_completion_rate"] == 1.0

    def test_zero_orders_misrouted(self):
        """5 个 0 订单场景 3 个返 ASK_LOGIN_OR_LIST + 2 个返 DIRECT_ANSWER → 60%"""
        _reset_m14()
        for _ in range(3):
            metrics.inc_resolver_decision(
                action="ask_login_or_list", total_orders=0,
                card_sent=False, card_expected=False,
            )
        for _ in range(2):
            metrics.inc_resolver_decision(
                action="direct_answer", total_orders=0,
                card_sent=False, card_expected=False,
            )
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["no_order_no_completion_rate"] == 0.6

    def test_no_zero_orders_returns_zero(self):
        """total_orders_zero=0 → 比率返 0（避免除零）"""
        _reset_m14()
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["no_order_no_completion_rate"] == 0.0


# =============================================================
# 3. card_triggered_when_expected_rate
# =============================================================
class TestCardTriggeredWhenExpectedRate:
    """SSE meta.card 应发且实际发的比例（plan 阈值 ≥ 95%）"""

    def test_card_sent_perfect(self):
        """10 次 card_expected 全 actual sent → 100%"""
        _reset_m14()
        for _ in range(10):
            metrics.inc_resolver_decision(
                action="show_picker", total_orders=3,
                card_sent=True, card_expected=True,
            )
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["card_triggered_when_expected_rate"] == 1.0

    def test_card_sent_partial(self):
        """10 次 card_expected 中 9 次 actual sent → 90%"""
        _reset_m14()
        for _ in range(9):
            metrics.inc_resolver_decision(
                action="show_picker", total_orders=3,
                card_sent=True, card_expected=True,
            )
        metrics.inc_resolver_decision(
            action="show_picker", total_orders=3,
            card_sent=False, card_expected=True,  # 应发但未发（异常）
        )
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["card_triggered_when_expected_rate"] == 0.9

    def test_card_sent_extras_not_counted(self):
        """card_sent=True 但 card_expected=False → 分子只计 expected=True ∩ sent=True 的次数；
        错配的 sent 不计入分子（也不计入分母）"""
        _reset_m14()
        # card_expected=True × 5, actual sent 5（应发且实发 → 计入分子）
        for _ in range(5):
            metrics.inc_resolver_decision(
                action="show_picker", total_orders=3,
                card_sent=True, card_expected=True,
            )
        # card_expected=False × 3, actual sent 3（错配，不计入分子/分母）
        for _ in range(3):
            metrics.inc_resolver_decision(
                action="direct_answer", total_orders=1,
                card_sent=True, card_expected=False,  # 错配，不计分子分母
            )
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["card_triggered_when_expected_rate"] == 1.0
        assert snap["card_expected"] == 5
        # 分子：5（只有 expected=True ∩ sent=True 计入）
        assert snap["card_sent_when_expected"] == 5

    def test_no_card_expected_returns_zero(self):
        """card_expected=0 → 比率返 0（避免除零）"""
        _reset_m14()
        snap = metrics.snapshot()["m14_resolver"]
        assert snap["ratios"]["card_triggered_when_expected_rate"] == 0.0


# =============================================================
# 4. 综合场景：混合决策 → 验证 4 指标联动
# =============================================================
class TestMixedScenario:
    """模拟真实流量：10 个 order_query 请求覆盖 0/1/N 决策"""

    def test_realistic_traffic_pattern(self):
        """10 个决策：3 个 N=多订单 + 5 个 N=1 + 2 个 0 订单
        期望：
        - multi_order_disambiguation_accuracy = 3/3 = 100%（全 SHOW_PICKER）
        - no_order_no_completion_rate = 2/2 = 100%
        - card_triggered_when_expected_rate = 8/8 = 100%（5 个 N=1 mini + 3 个 N=多 list）
        """
        _reset_m14()

        # 3 个多订单 → 全 SHOW_PICKER + card
        for _ in range(3):
            metrics.inc_resolver_decision(
                action="show_picker", total_orders=3,
                card_sent=True, card_expected=True,
            )

        # 5 个 1 订单 → DIRECT_ANSWER + mini card（按 SSE_CARD_V2=True 期望）
        for _ in range(5):
            metrics.inc_resolver_decision(
                action="direct_answer", total_orders=1,
                card_sent=True, card_expected=True,
            )

        # 2 个 0 订单 → ASK_LOGIN_OR_LIST（无 card）
        for _ in range(2):
            metrics.inc_resolver_decision(
                action="ask_login_or_list", total_orders=0,
                card_sent=False, card_expected=False,
            )

        snap = metrics.snapshot()["m14_resolver"]
        ratios = snap["ratios"]

        # 4 类指标全部 100%（理想流量）
        assert ratios["multi_order_disambiguation_accuracy"] == 1.0
        assert ratios["proactive_list_order_accuracy"] == 1.0
        assert ratios["no_order_no_completion_rate"] == 1.0
        assert ratios["card_triggered_when_expected_rate"] == 1.0

        # 计数校验
        assert snap["resolver_total"] == 10
        assert snap["by_action"]["show_picker"] == 3
        assert snap["by_action"]["direct_answer"] == 5
        assert snap["by_action"]["ask_login_or_list"] == 2
        assert snap["by_total_orders"]["zero"] == 2
        assert snap["by_total_orders"]["one"] == 5
        assert snap["by_total_orders"]["many"] == 3
        assert snap["card_expected"] == 8
        assert snap["card_sent_when_expected"] == 8

    def test_degraded_scenario(self):
        """降级流量：2 个 N=多但只 1 个 SHOW_PICKER（漏检）+ 1 个 N=多走了 DIRECT_ANSWER
        期望：
        - multi_order_disambiguation_accuracy = 1/2 = 0.5
        """
        _reset_m14()
        # 2 个 N=多，1 正确 1 错误
        metrics.inc_resolver_decision(
            action="show_picker", total_orders=2,
            card_sent=True, card_expected=True,
        )
        metrics.inc_resolver_decision(
            action="direct_answer", total_orders=2,  # 错误：应 SHOW_PICKER
            card_sent=False, card_expected=True,
        )

        snap = metrics.snapshot()["m14_resolver"]
        ratios = snap["ratios"]
        assert ratios["multi_order_disambiguation_accuracy"] == 0.5
        # card 应发 2 次，实发 1 次
        assert ratios["card_triggered_when_expected_rate"] == 0.5