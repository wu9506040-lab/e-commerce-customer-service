"""
闲鱼 MCP 集成配置

为什么单独一个模块：
- 智能客服 .env 已经很多配置，再加就拥挤
- 闲鱼相关配置集中一处：路径 / 命令 / 超时 / 开关
- 测试可 monkeypatch 单个变量，不动 .env
"""
import os
import shutil
import sys
from pathlib import Path


def _default_goofish_cmd() -> str:
    """默认 MCP 命令：自动检测 pip Scripts 目录（兼容 Windows Store Python）

    Windows Store Python 特殊布局（pip install --user 默认）：
        python.exe    → C:\\Users\\...\\Microsoft\\WindowsApps\\python.exe (shim)
        actual install → C:\\Users\\...\\Packages\\...\\LocalCache\\local-packages\\Python{ver}
        goofish-mcp.exe → ...\\local-packages\\Python{ver}\\Scripts\\goofish-mcp.exe
    sys.executable.parent\\Scripts 找不到（shim 不在那里）

    优先级（首个存在的路径返回）：
    1. 环境变量 XIANYU_MCP_CMD 显式覆盖（最高优先级，在 module 级）
    2. Windows Store Python user Scripts（site.getuserbase() + py_ver + Scripts）
    3. 标准 Python sys.executable 同级 Scripts/
    4. PATH 查找（shutil.which）
    5. 兜底字符串 "goofish-mcp"（最后 FileNotFoundError 但错误更清晰）
    """
    candidates: list[Path] = []
    # Windows Store Python：site.getuserbase() 下有 Python{ver}/Scripts/
    try:
        import site

        py_ver = f"Python{sys.version_info.major}{sys.version_info.minor}"
        candidates.append(
            Path(site.getuserbase()) / py_ver / "Scripts" / "goofish-mcp.exe"
        )
    except Exception:
        pass
    # 标准 Python 安装：sys.executable 同级 Scripts/
    candidates.append(Path(sys.executable).parent / "Scripts" / "goofish-mcp.exe")
    # PATH 查找
    which = shutil.which("goofish-mcp")
    if which:
        candidates.append(Path(which))
    for c in candidates:
        if c.exists():
            return str(c)
    return "goofish-mcp"  # 兜底


# ---- MCP server 启动命令 ----
# goofish-mcp 是 goofish-cli 包的 stdio MCP server 入口
# 通过 `pip install goofish-cli` 安装后会在 Scripts/ 下生成 goofish-mcp.exe
# Windows 上 pip install 默认不把 Scripts/ 加到 PATH —— 自动检测同 Python 环境的 Scripts/
# 也可设 XIANYU_MCMD_CMD 环境变量显式覆盖
GOOFISH_MCP_CMD: str = os.environ.get("XIANYU_MCP_CMD", _default_goofish_cmd())

# ---- cookie 文件路径 ----
# 闲鱼 cookie 通过 `goofish auth login <cookies.json>` 导入
# 默认 goofish 写到 ~/.goofish-cli/cookies.json（与 goofish-cli 默认一致）
# 容器场景可设环境变量 GOOFISH_COOKIE_PATH=/run/secrets/goofish-cookies.json
GOOFISH_COOKIE_PATH: str = os.environ.get(
    "GOOFISH_COOKIE_PATH",
    str(Path.home() / ".goofish-cli" / "cookies.json"),
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