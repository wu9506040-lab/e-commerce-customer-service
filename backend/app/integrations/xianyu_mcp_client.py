"""
闲鱼 MCP sync client — 智能客服通过 MCP 协议调 goofish-cli 操作闲鱼 IM

按 CLAUDE.md §9.2.2 模块隔离：
- 本模块只做 MCP 协议翻译（goofish 命令 ↔ MCP tool 名 + args 白名单）
- 不实现业务规则（业务规则在 tools/registry.py 的 runner）
- 不调 LLM，不做 RAG

调用链路：
  ToolRunner (sync) → XianyuMCPClient.list_conversations()
                    → asyncio.run(self._call_mcp_async('message_list_chats', {'limit': N}))
                    → mcp.ClientSession.call_tool(...)
                    → subprocess 'goofish-mcp' (stdio MCP server)
                    → 闲鱼 IM/HTTP API

降级策略（**核心：不抛异常，全返 error dict**）：
- ENABLE_XIANYU_TOOLS=False → 短路 {"error": "xianyu_tools_disabled"}
- cookie 文件不存在 → {"error": "xianyu_cookie_missing"}
- mcp SDK 未装 → {"error": "mcp_sdk_unavailable"}
- subprocess 启动失败 → {"error": "mcp_startup_failed"}
- MCP 调用超时 → {"error": "mcp_call_timeout"}
- MCP 业务错误 → {"error": "mcp_tool_error: ..."}
- 任意未捕获异常 → {"error": "xianyu_<method>_failed: ..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from app.core.config import settings
from app.integrations.xianyu_config import (
    GOOFISH_COOKIE_PATH,
    GOOFISH_MCP_CMD,
    XIANYU_MCP_CALL_TIMEOUT,
    XIANYU_MCP_STARTUP_TIMEOUT,
)

logger = logging.getLogger(__name__)

# mcp SDK 是动态 import：ENABLE_XIANYU_TOOLS=False 时不强制依赖
try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    _MCP_AVAILABLE = True
except ImportError:  # pragma: no cover — 缺包时降级
    _MCP_AVAILABLE = False
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None


# 业务方法 → MCP tool 名 + args 白名单
# MCP tool 名实测 = goofish 命令把 `-` 替成 `_`（如 `message list-chats` → `message_list_chats`）
_TOOL_MAP: dict[str, tuple[str, set[str]]] = {
    "list_conversations": ("message_list_chats", {"limit"}),
    "conversation_history": ("message_history", {"cid", "pages"}),
    "get_item": ("item_get", {"item_id"}),
    "list_my_items": ("item_list", set()),
    "send_message": ("message_send", {"cid", "toid", "text", "image"}),
}


class XianyuMCPClient:
    """闲鱼 MCP sync client（每次调用启动一次 session，避免跨协程状态污染）

    设计取舍（V16）：
    - 每次调用新建 stdio_client + session：subprocess 启动 ~0.5s，慢但稳；
      持久 session 在 FastAPI 多请求场景需 connection pool，V16.x 优化
    - sync 入口（agent_runner 是 sync 函数）；内部 asyncio.run() 跑 async MCP SDK
    - 超时分两段：启动（15s）+ 业务调用（30s）；超时即降级，不重试
    - 灰度开关 + cookie + SDK 三段短路，确保 ENABLE=False 时零开销
    """

    def __init__(
        self,
        server_cmd: Optional[str] = None,
        cookie_path: Optional[str] = None,
        call_timeout: Optional[float] = None,
        startup_timeout: Optional[float] = None,
    ):
        self._cmd = server_cmd or GOOFISH_MCP_CMD
        self._cookie_path = cookie_path or GOOFISH_COOKIE_PATH
        self._call_timeout = call_timeout or XIANYU_MCP_CALL_TIMEOUT
        self._startup_timeout = startup_timeout or XIANYU_MCP_STARTUP_TIMEOUT

    # =============================================================
    # 公开 sync API（智能客服 tool runner 调这些）
    # =============================================================
    def is_enabled(self) -> bool:
        """灰度开关 + SDK 可用 + cookie 存在（三段短路）"""
        if not settings.ENABLE_XIANYU_TOOLS:
            return False
        if not _MCP_AVAILABLE:
            return False
        if not os.path.exists(self._cookie_path):
            return False
        return True

    def list_conversations(self, limit: int = 20) -> dict:
        return self._call("list_conversations", {"limit": limit})

    def get_conversation_history(self, cid: str, pages: int = 1) -> dict:
        return self._call("conversation_history", {"cid": cid, "pages": pages})

    def get_item(self, item_id: str) -> dict:
        return self._call("get_item", {"item_id": item_id})

    def list_my_items(self) -> dict:
        return self._call("list_my_items", {})

    def send_message(
        self, cid: str, toid: str, text: str, image_url: Optional[str] = None
    ) -> dict:
        """向闲鱼会话发送消息（**写操作**）

        Args:
            cid: 闲鱼会话 ID（= conversation_id）
            toid: 接收方 user_id（对方的 unb，不是 seller 自己的）
            text: 消息正文
            image_url: 可选，图片 URL
        """
        args: dict = {"cid": cid, "toid": toid, "text": text}
        if image_url:
            args["image"] = image_url
        return self._call("send_message", args)

    def health_check(self) -> dict:
        """健康检查：开关 + SDK + cookie + 命令可执行性（不真起 subprocess）"""
        return {
            "enabled": settings.ENABLE_XIANYU_TOOLS,
            "mcp_sdk_available": _MCP_AVAILABLE,
            "cookie_exists": os.path.exists(self._cookie_path),
            "cookie_path": self._cookie_path,
            "mcp_cmd": self._cmd,
            "is_enabled": self.is_enabled(),
        }

    # =============================================================
    # 内部
    # =============================================================
    def _call(self, method: str, args: dict) -> dict:
        """sync 入口：灰度短路 + args 白名单 + 异常降级

        线程隔离 event loop（**解决"asyncio.run() cannot be called from
        a running event loop"**）：

        - 智能客服是 FastAPI（async web）；每个请求在 async context 跑
        - agent_runner → dispatch → _run_xianyu_* → client._call 这一路是同步的
        - 如果直接 asyncio.run()：会触发 RuntimeError，因为外层已有 running loop
        - 用 ThreadPoolExecutor(1) 把 MCP call 丢到独立线程：thread 内没有
          outer loop，asyncio.run() 合法

        设计取舍（V16）：
        - 单飞线程（max_workers=1）= 不并发（避免 cookie 文件被并发读触发闲鱼风控）
        - 每个方法调用新建 thread（MCP call 频率低，subprocess 启动 ~0.5s 可接受）
        - 未来 V16.x 可优化：复用持久 thread + queue（待闲鱼 IM 长连接 watch 上线时）
        """
        if not settings.ENABLE_XIANYU_TOOLS:
            return {"error": "xianyu_tools_disabled"}
        if not _MCP_AVAILABLE:
            return {"error": "mcp_sdk_unavailable"}
        if not os.path.exists(self._cookie_path):
            return {"error": "xianyu_cookie_missing"}
        if method not in _TOOL_MAP:
            return {"error": f"unknown_method: {method}"}
        tool_name, valid_args = _TOOL_MAP[method]
        clean_args = {k: v for k, v in args.items() if k in valid_args}
        try:
            return self._run_in_fresh_thread(tool_name, clean_args)
        except asyncio.TimeoutError:
            logger.warning(
                f"xianyu_mcp.{method} timeout ({self._startup_timeout + self._call_timeout}s)",
                extra={"intent": "xianyu_mcp", "method": method},
            )
            return {"error": "mcp_call_timeout"}
        except Exception as e:
            logger.exception(
                f"xianyu_mcp.{method} failed: {type(e).__name__}: {e}",
                extra={"intent": "xianyu_mcp", "method": method},
            )
            return {
                "error": f"xianyu_{method}_failed: {type(e).__name__}: {str(e)[:200]}"
            }

    def _run_in_fresh_thread(self, tool_name: str, args: dict) -> dict:
        """在独立线程跑 asyncio.run()，隔离外层 event loop

        失败模式：
        - thread 内 startup/call 超时 → 抛 concurrent.futures.TimeoutError
        - thread 内未处理异常 → 抛回主线程，_call 兜住
        """
        import concurrent.futures

        total_timeout = self._startup_timeout + self._call_timeout + 5
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="xianyu-mcp"
        ) as executor:
            future = executor.submit(
                lambda: asyncio.run(
                    asyncio.wait_for(
                        self._call_mcp_async(tool_name, args),
                        timeout=self._startup_timeout + self._call_timeout,
                    )
                )
            )
            return future.result(timeout=total_timeout)

    async def _call_mcp_async(self, tool_name: str, args: dict) -> dict:
        """async 入口：起 subprocess + handshake + call_tool + 解析 result"""
        server_params = StdioServerParameters(
            command=self._cmd,
            args=[],
            env={**os.environ, "GOOFISH_COOKIE_PATH": self._cookie_path},
        )
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(
                    session.initialize(),
                    timeout=self._startup_timeout,
                )
                # list_tools 跳过：tool 名是静态已知（_TOOL_MAP）
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, args),
                    timeout=self._call_timeout,
                )
                if result.isError:
                    # MCP 业务错误（不是 connection 错）
                    err_text = ""
                    if result.content and hasattr(result.content[0], "text"):
                        err_text = result.content[0].text
                    return {
                        "error": f"mcp_tool_error: {err_text[:200]}"
                    }
                # 提取 text content（goofish --format json 时 text 是 JSON 字符串）
                texts = [
                    c.text
                    for c in (result.content or [])
                    if hasattr(c, "text")
                ]
                if not texts:
                    return {"ok": True, "raw": ""}
                text = texts[0]
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return {"raw": text}


# =============================================================
# 单例（lazy init；settings 变化可通过 reset 强制重建）
# =============================================================
_client_instance: Optional[XianyuMCPClient] = None


def get_xianyu_client() -> XianyuMCPClient:
    """获取全局单例 client"""
    global _client_instance
    if _client_instance is None:
        _client_instance = XianyuMCPClient()
    return _client_instance


def reset_xianyu_client() -> None:
    """重置单例（测试用 / cookie 重新登录后）"""
    global _client_instance
    _client_instance = None


__all__ = [
    "XianyuMCPClient",
    "get_xianyu_client",
    "reset_xianyu_client",
]