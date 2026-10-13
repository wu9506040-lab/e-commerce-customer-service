"""V15：用户上下文预加载服务单测

覆盖：
- preload() 加载订单 + 画像
- preload() 匿名用户返空
- preload() 单字段失败不影响其他字段
- to_prompt_block() 格式化输出
- to_prompt_block() 硬上限截断
- to_prompt_block() 空输入返空串
"""
import pytest
from unittest.mock import MagicMock, patch

from app.services import user_context_preloader as ucp


class TestPreload:
    """preload() 主路径"""

    def test_anonymous_user_returns_empty(self):
        """匿名用户（user_id=0）→ 返空"""
        result = ucp.preload(0)
        assert result == {"orders": [], "profile_text": ""}

    def test_none_user_id_returns_empty(self):
        """None → 返空"""
        assert ucp.preload(None) == {"orders": [], "profile_text": ""}

    def test_anonymous_constant_returns_empty(self):
        """ANONYMOUS_USER_ID（通常=0）→ 返空"""
        from app.services.session_service import ANONYMOUS_USER_ID
        assert ucp.preload(ANONYMOUS_USER_ID) == {"orders": [], "profile_text": ""}

    def test_loads_orders_with_items(self):
        """正常用户 → 加载订单 + 产品明细（不同订单用 side_effect 区分明细）"""
        fake_orders = [
            {
                "order_no": "ORD20260601001",
                "status": "shipped",
                "status_zh": "已发货",
                "total_amount": 99.0,
            },
            {
                "order_no": "ORD20260605002",
                "status": "completed",
                "status_zh": "已完成",
                "total_amount": 128.0,
            },
        ]

        # 按 order_no 返回不同明细
        def fake_get_detail(user_id, order_no):
            return {
                "ORD20260601001": {"items": [{"product_name": "ZP1 蓝牙耳机", "qty": 1}]},
                "ORD20260605002": {"items": [{"product_name": "ZP2 鼠标", "qty": 2}]},
            }[order_no]

        with patch.object(ucp.OrderService, "list_user_orders", return_value=fake_orders), \
             patch.object(ucp.OrderService, "get_order_detail", side_effect=fake_get_detail), \
             patch.object(ucp.profile_service, "get_or_create", return_value=None):
            result = ucp.preload(user_id=123)

        assert len(result["orders"]) == 2
        assert result["orders"][0]["order_no"] == "ORD20260601001"
        assert result["orders"][0]["items_text"] == "ZP1 蓝牙耳机×1"
        assert result["orders"][1]["items_text"] == "ZP2 鼠标×2"

    def test_orders_capped_at_max(self):
        """订单超过 MAX_RECENT_ORDERS → 截断"""
        fake_orders = [{"order_no": f"ORD{i:08d}", "status": "paid", "total_amount": 10.0}
                       for i in range(10)]

        with patch.object(ucp.OrderService, "list_user_orders", return_value=fake_orders), \
             patch.object(ucp.OrderService, "get_order_detail", return_value={"items": []}), \
             patch.object(ucp.profile_service, "get_or_create", return_value=None):
            result = ucp.preload(user_id=1)

        assert len(result["orders"]) == ucp.MAX_RECENT_ORDERS

    def test_orders_failure_does_not_break_profile(self):
        """订单加载失败 → 订单空但画像仍加载"""
        with patch.object(ucp.OrderService, "list_user_orders",
                          side_effect=RuntimeError("DB 挂了")), \
             patch.object(ucp.profile_service, "get_or_create", return_value=None):
            result = ucp.preload(user_id=1)

        assert result["orders"] == []  # 失败 → 空
        # profile_text 是空（get_or_create 返 None）

    def test_profile_failure_does_not_break_orders(self):
        """画像加载失败 → 画像空但订单仍加载"""
        fake_orders = [{"order_no": "ORD001", "status": "paid", "total_amount": 10.0}]

        with patch.object(ucp.OrderService, "list_user_orders", return_value=fake_orders), \
             patch.object(ucp.OrderService, "get_order_detail", return_value={"items": []}), \
             patch.object(ucp.profile_service, "get_or_create",
                          side_effect=RuntimeError("DB 挂了")):
            result = ucp.preload(user_id=1)

        assert len(result["orders"]) == 1
        assert result["profile_text"] == ""


class TestToPromptBlock:
    """to_prompt_block() 格式化"""

    def test_empty_input_returns_empty(self):
        """空 dict → 返空串"""
        assert ucp.to_prompt_block({}) == ""
        assert ucp.to_prompt_block({"orders": [], "profile_text": ""}) == ""

    def test_orders_only(self):
        """只有订单 → 输出订单段"""
        data = {
            "orders": [
                {
                    "order_no": "ORD001",
                    "status_zh": "已发货",
                    "total_amount": 99.0,
                    "items_text": "ZP1 蓝牙耳机×1",
                }
            ],
            "profile_text": "",
        }
        block = ucp.to_prompt_block(data)
        assert "ORD001" in block
        assert "已发货" in block
        assert "¥99" in block
        assert "ZP1 蓝牙耳机×1" in block
        assert "【会话上下文预加载】" in block  # 反幻觉前缀

    def test_profile_text_passes_through(self):
        """profile_text 直接拼接"""
        data = {
            "orders": [],
            "profile_text": "【当前用户画像】\n- 偏好：电子产品",
        }
        block = ucp.to_prompt_block(data)
        assert "【当前用户画像】" in block
        assert "电子产品" in block

    def test_hard_limit_truncates(self):
        """超过 max_len → 截断到 max_len"""
        # 构造一个超长订单明细
        long_items = " / ".join(f"商品{i}×1" for i in range(50))
        data = {
            "orders": [
                {
                    "order_no": "ORD001",
                    "status_zh": "已发货",
                    "total_amount": 99.0,
                    "items_text": long_items,
                }
            ],
            "profile_text": "",
        }
        block = ucp.to_prompt_block(data, max_len=100)
        assert len(block) <= 100
        assert block.endswith("…")

    def test_combined_orders_and_profile(self):
        """订单 + 画像都输出"""
        data = {
            "orders": [
                {"order_no": "ORD001", "status_zh": "已发货", "total_amount": 99.0,
                 "items_text": "ZP1×1"}
            ],
            "profile_text": "【当前用户画像】\n- 偏好：数码",
        }
        block = ucp.to_prompt_block(data)
        assert "ORD001" in block
        assert "ZP1×1" in block
        assert "数码" in block
        # 订单段在前，画像段在后
        assert block.index("ORD001") < block.index("数码")


class TestSimplifyItems:
    """_simplify_items 内部辅助"""

    def test_basic_format(self):
        items = [{"product_name": "ZP1", "qty": 1}, {"product_name": "ZP2", "qty": 2}]
        result = ucp._simplify_items(items)
        assert result == "ZP1×1 / ZP2×2"

    def test_capped_at_max(self):
        items = [{"product_name": f"P{i}", "qty": 1} for i in range(10)]
        result = ucp._simplify_items(items, max_items=3)
        # max_items=3 截断 + 10>3 加 "等10件"
        assert result == "P0×1 / P1×1 / P2×1 / 等10件"

    def test_etc_suffix_only_when_overflow(self):
        items = [{"product_name": f"P{i}", "qty": 1} for i in range(3)]
        result = ucp._simplify_items(items, max_items=3)
        # 刚好 3 个，不加 "等N件"
        assert "等" not in result
        assert result == "P0×1 / P1×1 / P2×1"

    def test_empty_items(self):
        assert ucp._simplify_items([]) == ""
