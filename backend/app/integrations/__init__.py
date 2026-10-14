"""
Integrations Layer - 第三方平台集成层（V16 · 闲鱼 MCP 接入）

按 CLAUDE.md §9.2.2 模块隔离：
- 本层只做第三方协议适配（如 MCP over stdio）
- 不实现业务逻辑（业务逻辑在 tools/registry.py 的 runner）
- 不直接 import service/tool，避免循环依赖

Sprint：V16 · 闲鱼 IM 渠道接入
- XianyuMCPClient：MCP stdio client，连 goofish-mcp 子进程
- get_xianyu_client()：单例（lazy init，避免重复启动 subprocess）
"""
from app.integrations.xianyu_mcp_client import XianyuMCPClient, get_xianyu_client

__all__ = ["XianyuMCPClient", "get_xianyu_client"]