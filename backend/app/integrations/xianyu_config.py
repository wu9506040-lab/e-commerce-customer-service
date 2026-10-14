"""
闲鱼 MCP 集成配置

为什么单独一个模块：
- 智能客服 .env 已经很多配置，再加就拥挤
- 闲鱼相关配置集中一处：路径 / 命令 / 超时 / 开关
- 测试可 monkeypatch 单个变量，不动 .env
"""
import os
from pathlib import Path

# ---- MCP server 启动命令 ----
# goofish-mcp 是 goofish-cli 包的 stdio MCP server 入口
# 通过 `pip install goofish-cli` 安装后会在 Scripts/ 下生成 goofish-mcp.exe
# 默认用 PATH 查找；如果在 PATH 找不到，可以设 XIANYU_MCP_CMD 绝对路径
GOOFISH_MCP_CMD: str = os.environ.get(
    "XIANYU_MCP_CMD",
    "goofish-mcp",  # 默认走 PATH（pyinstaller 安装的 Scripts/ 目录）
)

# ---- cookie 文件路径 ----
# 闲鱼 cookie 通过 `goofish auth login <cookies.json>` 导入
# 默认放用户家目录；容器场景可设 /run/GOOFISH_COOKIES 之类
GOOFISH_COOKIE_PATH: str = os.environ.get(
    "GOOFISH_COOKIE_PATH",
    str(Path.home() / "goofish-cookies.json"),
)

# ---- MCP 单次调用超时（秒）----
# 闲鱼 IM 接口正常 <5s；遇风控/限流可能 >30s
# 超时后 client 返 timeout error dict（不抛异常），dispatch 容错
XIANYU_MCP_CALL_TIMEOUT: float = float(
    os.environ.get("XIANYU_MCP_CALL_TIMEOUT", "30.0")
)

# ---- MCP subprocess 启动超时（秒）----
# 首次启动 goofish-mcp + MCP handshake 协议握手，最坏 ~10s
XIANYU_MCP_STARTUP_TIMEOUT: float = float(
    os.environ.get("XIANYU_MCP_STARTUP_TIMEOUT", "15.0")
)