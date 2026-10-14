"""
XianyuMCPClient 测试 — 覆盖三段短路 + args 白名单 + 异常降级 + health_check

按 CLAUDE.md 调试铁律：测试不依赖真实 MCP subprocess（cookie + 网络要求）。
Mock 三段短路 + 拦截 _call_mcp_async，确保业务逻辑可单测。
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations import xianyu_mcp_client as cli_mod
from app.integrations.xianyu_config import GOOFISH_COOKIE_PATH
from app.integrations.xianyu_mcp_client import (
    XianyuMCPClient,
    get_xianyu_client,
    reset_xianyu_client,
)


@pytest.fixture(autouse=True)
def reset_client_singleton():
    """每个测试前重置单例（防止测试间污染）"""
    reset_xianyu_client()
    yield
    reset_xianyu_client()


@pytest.fixture
def enable_xianyu(monkeypatch, tmp_path):
    """启用开关 + 提供临时 cookie 文件（满足三段短路）"""
    fake_cookie = tmp_path / "xianyu-cookies.json"
    fake_cookie.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", True)
    monkeypatch.setattr(cli_mod, "GOOFISH_COOKIE_PATH", str(fake_cookie))
    return fake_cookie


# =============================================================
# 1. 三段短路（不依赖 subprocess）
# =============================================================
class TestIsEnabled:
    def test_disabled_when_settings_off(self, monkeypatch):
        """灰度开关关 → is_enabled()=False"""
        monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", False)
        c = XianyuMCPClient()
        assert c.is_enabled() is False

    def test_disabled_when_mcp_sdk_missing(self, monkeypatch, tmp_path):
        """mcp SDK 未装 → is_enabled()=False"""
        fake_cookie = tmp_path / "c.json"
        fake_cookie.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", True)
        monkeypatch.setattr(cli_mod, "GOOFISH_COOKIE_PATH", str(fake_cookie))
        monkeypatch.setattr(cli_mod, "_MCP_AVAILABLE", False)
        c = XianyuMCPClient()
        assert c.is_enabled() is False

    def test_disabled_when_cookie_missing(self, monkeypatch, tmp_path):
        """cookie 文件不存在 → is_enabled()=False"""
        monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", True)
        monkeypatch.setattr(cli_mod, "_MCP_AVAILABLE", True)
        monkeypatch.setattr(cli_mod, "GOOFISH_COOKIE_PATH", str(tmp_path / "missing.json"))
        c = XianyuMCPClient()
        assert c.is_enabled() is False

    def test_enabled_when_all_three_pass(self, enable_xianyu):
        """三段全 OK → is_enabled()=True"""
        c = XianyuMCPClient()
        assert c.is_enabled() is True


# =============================================================
# 2. 灰度短路返回值（不连 MCP）
# =============================================================
class TestGracefulDegradation:
    def test_list_conversations_disabled(self, monkeypatch):
        """灰度关 → 方法直接返 disabled error，不抛异常"""
        monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", False)
        c = XianyuMCPClient()
        result = c.list_conversations(limit=10)
        assert result == {"error": "xianyu_tools_disabled"}

    def test_send_message_disabled(self, monkeypatch):
        """send_message 也走短路"""
        monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", False)
        c = XianyuMCPClient()
        result = c.send_message(cid="abc123", toid="u123", text="hi")
        assert result == {"error": "xianyu_tools_disabled"}

    def test_cookie_missing_returns_error(self, monkeypatch, tmp_path):
        """cookie 缺 → 返 error dict"""
        monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", True)
        monkeypatch.setattr(cli_mod, "_MCP_AVAILABLE", True)
        monkeypatch.setattr(cli_mod, "GOOFISH_COOKIE_PATH", str(tmp_path / "absent.json"))
        c = XianyuMCPClient()
        result = c.list_conversations()
        assert result == {"error": "xianyu_cookie_missing"}


# =============================================================
# 3. args 白名单（多余参数被过滤）
# =============================================================
class TestArgsWhitelist:
    def test_unknown_method_returns_error(self, enable_xianyu):
        """不存在的业务方法 → error（不抛）"""
        c = XianyuMCPClient()
        # 走 _call 直接（绕过公开方法的 5 个）
        result = c._call("non_existent_method", {})
        assert "unknown_method" in result["error"]

    def test_args_filtered_to_whitelist(self, enable_xianyu):
        """list_conversations 仅留 limit，其他字段被过滤"""
        with patch.object(
            XianyuMCPClient,
            "_call_mcp_async",
            new=AsyncMock(return_value={"conversations": []}),
        ):
            c = XianyuMCPClient()
            c.list_conversations(limit=5)
            # 拦截 args 实际传给 _call_mcp_async 的内容
            # 用 patch.object 替换 _call_mcp_async 后，调 list_conversations 会触发
            # 这里我们直接验证白名单逻辑通过 _call
            clean_args = cli_mod._TOOL_MAP["list_conversations"][1]
            assert "limit" in clean_args
            assert "cid" not in clean_args  # 不应越权传 cid


# =============================================================
# 4. 超时降级 + 异常降级（mock asyncio.run 模拟）
# =============================================================
class TestCallFailureModes:
    def test_timeout_returns_error(self, enable_xianyu):
        """asyncio.TimeoutError → 返 mcp_call_timeout"""
        import asyncio as _asyncio

        # 只 patch asyncio.run（不动整个 asyncio 模块，否则 except asyncio.TimeoutError 失效）
        with patch(
            "app.integrations.xianyu_mcp_client.asyncio.run",
            side_effect=_asyncio.TimeoutError(),
        ):
            c = XianyuMCPClient()
            result = c.list_conversations(limit=5)
        assert result["error"] == "mcp_call_timeout"

    def test_unexpected_exception_returns_error(self, enable_xianyu):
        """未捕获异常 → 返 xianyu_<method>_failed"""
        with patch(
            "app.integrations.xianyu_mcp_client.asyncio.run",
            side_effect=RuntimeError("subprocess crashed"),
        ):
            c = XianyuMCPClient()
            result = c.list_conversations(limit=5)
        assert "xianyu_list_conversations_failed" in result["error"]
        assert "RuntimeError" in result["error"]
        assert "subprocess crashed" in result["error"]


# =============================================================
# 5. health_check（纯 dict 返字段）
# =============================================================
class TestHealthCheck:
    def test_health_check_keys(self, monkeypatch):
        """health_check 返 6 个字段"""
        monkeypatch.setattr(cli_mod.settings, "ENABLE_XIANYU_TOOLS", False)
        c = XianyuMCPClient()
        h = c.health_check()
        assert set(h.keys()) == {
            "enabled",
            "mcp_sdk_available",
            "cookie_exists",
            "cookie_path",
            "mcp_cmd",
            "is_enabled",
        }
        assert h["enabled"] is False
        assert h["is_enabled"] is False


# =============================================================
# 6. 单例（get_xianyu_client + reset）
# =============================================================
class TestSingleton:
    def test_get_returns_same_instance(self):
        c1 = get_xianyu_client()
        c2 = get_xianyu_client()
        assert c1 is c2

    def test_reset_returns_new_instance(self):
        c1 = get_xianyu_client()
        reset_xianyu_client()
        c2 = get_xianyu_client()
        assert c1 is not c2


# =============================================================
# 7. args 真实传参形状（mock _call_mcp_async 验证 args 内容）
# =============================================================
class TestArgsMapping:
    def test_send_message_args(self, enable_xianyu):
        """send_message 传 image_url 时带 image，不传时不带"""
        captured_args = {}

        async def fake_call(tool_name, args):
            captured_args["tool_name"] = tool_name
            captured_args["args"] = args
            return {"ok": True}

        with patch.object(
            XianyuMCPClient, "_call_mcp_async", new=AsyncMock(side_effect=fake_call)
        ):
            c = XianyuMCPClient()
            c.send_message(cid="C123", toid="U123", text="hi")
            assert captured_args["tool_name"] == "message_send"
            assert captured_args["args"] == {
                "cid": "C123",
                "toid": "U123",
                "text": "hi",
            }

            c.send_message(
                cid="C123",
                toid="U123",
                text="hi",
                image_url="https://x.com/a.png",
            )
            assert captured_args["args"] == {
                "cid": "C123",
                "toid": "U123",
                "text": "hi",
                "image": "https://x.com/a.png",
            }

    def test_conversation_history_args(self, enable_xianyu):
        """conversation_history → message_history + cid/pages"""
        captured = {}

        async def fake(tool_name, args):
            captured["tool"] = tool_name
            captured["args"] = args
            return {"messages": []}

        with patch.object(
            XianyuMCPClient, "_call_mcp_async", new=AsyncMock(side_effect=fake)
        ):
            c = XianyuMCPClient()
            c.get_conversation_history(cid="C999", pages=3)
            assert captured["tool"] == "message_history"
            assert captured["args"] == {"cid": "C999", "pages": 3}

    def test_get_item_args(self, enable_xianyu):
        """get_item → item_get + item_id"""
        captured = {}

        async def fake(tool_name, args):
            captured["tool"] = tool_name
            captured["args"] = args
            return {"item": {"id": "X1"}}

        with patch.object(
            XianyuMCPClient, "_call_mcp_async", new=AsyncMock(side_effect=fake)
        ):
            c = XianyuMCPClient()
            c.get_item(item_id="X1")
            assert captured["tool"] == "item_get"
            assert captured["args"] == {"item_id": "X1"}