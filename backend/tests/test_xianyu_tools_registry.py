"""
闲鱼工具注册测试 — 验证 5 个 xianyu_* ToolSpec 已在 REGISTRY + dispatch 路由正确

按 CLAUDE.md §9.7：测试不依赖真实 MCP（mock get_xianyu_client）
"""
from unittest.mock import MagicMock, patch

import pytest

from app.tools.registry import (
    REGISTRY,
    ToolContext,
    dispatch,
    to_openai_tools,
)


@pytest.fixture
def mock_client():
    """module 级 mock_client：所有 dispatch 测试复用"""
    with patch("app.integrations.get_xianyu_client") as m:
        client = MagicMock()
        client.is_enabled.return_value = True  # 绕过 client 内部灰度
        client.list_conversations.return_value = {"conversations": [{"cid": "C1"}]}
        client.get_conversation_history.return_value = {"messages": [{"role": "mid"}]}
        client.get_item.return_value = {"item": {"id": "X1"}}
        client.list_my_items.return_value = {"items": [{"id": "Y1"}]}
        client.send_message.return_value = {"ok": True, "message_id": "M1"}
        m.return_value = client
        yield client


# =============================================================
# 1. 5 个 ToolSpec 已注册 + 字段完整
# =============================================================
@pytest.mark.parametrize("tool_name", [
    "xianyu_list_conversations",
    "xianyu_conversation_history",
    "xianyu_get_item",
    "xianyu_list_my_items",
    "xianyu_send_message",
])
class TestRegistration:
    def test_registered(self, tool_name):
        assert tool_name in REGISTRY

    def test_name_matches_key(self, tool_name):
        """ToolSpec.name 必须与 REGISTRY key 一致"""
        assert REGISTRY[tool_name].name == tool_name

    def test_has_description(self, tool_name):
        """description 非空（LLM 据此判断调不调）"""
        assert REGISTRY[tool_name].description
        assert len(REGISTRY[tool_name].description) > 20

    def test_has_runner(self, tool_name):
        assert callable(REGISTRY[tool_name].runner)

    def test_parameters_schema_valid(self, tool_name):
        """parameters 是合法 JSON Schema（type=object）"""
        params = REGISTRY[tool_name].parameters
        assert params["type"] == "object"
        assert "properties" in params
        assert "required" in params


# =============================================================
# 2. to_openai_tools 输出包含 5 个 xianyu_*
# =============================================================
class TestOpenAIToolsExport:
    def test_xianyu_tools_in_openai_export(self):
        tools = to_openai_tools()
        names = {t["function"]["name"] for t in tools}
        for n in [
            "xianyu_list_conversations",
            "xianyu_conversation_history",
            "xianyu_get_item",
            "xianyu_list_my_items",
            "xianyu_send_message",
        ]:
            assert n in names, f"{n} 应在 to_openai_tools() 输出中"

    def test_openai_format_structure(self):
        """每个 tool 含 type=function / function.{name,description,parameters}"""
        tools = to_openai_tools()
        xianyu_tools = [t for t in tools if t["function"]["name"].startswith("xianyu_")]
        assert len(xianyu_tools) == 5
        for t in xianyu_tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]


# =============================================================
# 3. dispatch 路由（mock client）
# =============================================================
class TestDispatchRouting:
    """验证 dispatch(name, args_json, ctx) 路由到 xianyu_* runner + 调用 client**

    关键：灰度关时返 error dict（不调 client）—— 测两层
    """

    def test_list_conversations_dispatch(self, mock_client):
        result = dispatch(
            "xianyu_list_conversations",
            '{"limit": 5}',
            ToolContext(user_id=None),
        )
        mock_client.list_conversations.assert_called_once_with(limit=5)
        assert result == {"conversations": [{"cid": "C1"}]}

    def test_conversation_history_dispatch(self, mock_client):
        result = dispatch(
            "xianyu_conversation_history",
            '{"cid": "C123", "pages": 2}',
            ToolContext(user_id=None),
        )
        mock_client.get_conversation_history.assert_called_once_with(cid="C123", pages=2)
        assert result == {"messages": [{"role": "mid"}]}

    def test_get_item_dispatch(self, mock_client):
        result = dispatch(
            "xianyu_get_item",
            '{"item_id": "X999"}',
            ToolContext(user_id=None),
        )
        mock_client.get_item.assert_called_once_with(item_id="X999")
        assert result == {"item": {"id": "X1"}}

    def test_list_my_items_dispatch(self, mock_client):
        result = dispatch(
            "xianyu_list_my_items",
            "{}",
            ToolContext(user_id=None),
        )
        mock_client.list_my_items.assert_called_once_with()
        assert result == {"items": [{"id": "Y1"}]}


# =============================================================
# 4. 写操作必须 confirmed=true（与 modify_address 同模式）
# =============================================================
class TestWriteConfirmation:
    def test_send_message_without_confirmed_returns_preview(self, mock_client):
        """confirmed=false → 返 needs_confirmation + preview（不真发）"""
        with patch("app.integrations.get_xianyu_client") as m:
            client = MagicMock()
            client.is_enabled.return_value = True
            client.send_message.return_value = {"ok": True}
            m.return_value = client
            result = dispatch(
                "xianyu_send_message",
                '{"cid": "C123", "text": "您好，欢迎光临"}',
                ToolContext(user_id=None),
            )
        assert result["needs_confirmation"] is True
        assert result["preview"]["cid"] == "C123"
        assert result["preview"]["text"].startswith("您好")
        # 关键：未真发
        client.send_message.assert_not_called()

    def test_send_message_with_confirmed_calls_client(self, mock_client):
        """confirmed=true → 真调 client.send_message"""
        result = dispatch(
            "xianyu_send_message",
            '{"cid": "C123", "text": "hi", "confirmed": true}',
            ToolContext(user_id=None),
        )
        mock_client.send_message.assert_called_once_with(
            cid="C123", text="hi", image_url=None
        )
        assert result == {"ok": True, "message_id": "M1"}

    def test_send_message_with_image(self, mock_client):
        """confirmed=true + image → 透传 image_url"""
        dispatch(
            "xianyu_send_message",
            '{"cid": "C123", "text": "看图", "image": "https://x.com/a.png", "confirmed": true}',
            ToolContext(user_id=None),
        )
        mock_client.send_message.assert_called_once_with(
            cid="C123", text="看图", image_url="https://x.com/a.png"
        )

    def test_send_message_missing_cid_returns_error(self, mock_client):
        """cid 缺 → 返 error（不调 client）"""
        result = dispatch(
            "xianyu_send_message",
            '{"text": "hi"}',
            ToolContext(user_id=None),
        )
        assert "cid and text are required" in result["error"]
        mock_client.send_message.assert_not_called()

    def test_send_message_missing_text_returns_error(self, mock_client):
        result = dispatch(
            "xianyu_send_message",
            '{"cid": "C123"}',
            ToolContext(user_id=None),
        )
        assert "cid and text are required" in result["error"]


# =============================================================
# 5. 参数边界
# =============================================================
class TestParamEdge:
    def test_conversation_history_missing_cid(self, mock_client):
        """cid 缺 → error（不调 client）"""
        with patch("app.integrations.get_xianyu_client") as m:
            client = MagicMock()
            m.return_value = client
            result = dispatch(
                "xianyu_conversation_history",
                "{}",
                ToolContext(user_id=None),
            )
        assert "cid is required" in result["error"]

    def test_get_item_missing_item_id(self, mock_client):
        with patch("app.integrations.get_xianyu_client") as m:
            client = MagicMock()
            m.return_value = client
            result = dispatch(
                "xianyu_get_item",
                "{}",
                ToolContext(user_id=None),
            )
        assert "item_id is required" in result["error"]


# =============================================================
# 6. 灰度关闭时 dispatch 返 disabled error（端到端）
# =============================================================
class TestGreyRolloutViaDispatch:
    def test_dispatch_when_disabled_returns_disabled_error(self, monkeypatch):
        """settings.ENABLE_XIANYU_TOOLS=False → dispatch 返 disabled"""
        from app.integrations import xianyu_mcp_client as cli_mod

        monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", False)
        monkeypatch.setattr(cli_mod, "_MCP_AVAILABLE", True)

        result = dispatch(
            "xianyu_list_conversations",
            '{"limit": 5}',
            ToolContext(user_id=None),
        )
        assert result == {"error": "xianyu_tools_disabled"}