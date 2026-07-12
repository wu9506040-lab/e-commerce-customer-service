"""
Prompt 加载器 — Sprint 2 基础设施

业务模块通过 `get_prompt_loader().load(name)` 读取 prompt，
不再在业务代码中写 f-string / 三引号字符串字面量。

设计要点（CLAUDE.md §9.6 + Sprint 2 §3.3）：
- Protocol 优先：业务依赖 `PromptLoader` 抽象，不耦合实现
- name 白名单 + 路径 resolve 防越权（拒绝 `../` 注入）
- mtime 缓存：文件被改后下次 load 自动返回新值（不需重启）
- 单进程读多写少 → threading.Lock 保护 dict；写并发留 V3+

不范围：
- 不做 prompt 版本管理 / 灰度 / DB 存储（V3+）
- 不做租户级覆盖（S6 范围）
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Dict, Optional, Protocol, Tuple, runtime_checkable

import yaml


# =============================================================
# 自定义业务异常（就近定义，避免污染 core/）
# =============================================================
class PromptError(Exception):
    """Prompt 加载失败的基类异常。"""


class PromptNameError(PromptError):
    """prompt name 不合法（含路径分隔符、'..'、非法字符）。"""


class PromptNotFoundError(PromptError):
    """prompt 文件不存在。"""


class PromptFormatError(PromptError):
    """prompt YAML 解析失败或内容缺失。"""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"prompt 格式错误 [{name}]: {reason}")
        self.name = name
        self.reason = reason


# =============================================================
# Protocol（CLAUDE.md §9.3.3 — 业务模块靠此抽象，不直接 new）
# =============================================================
_NAME_PATTERN = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*$")


@runtime_checkable
class PromptLoader(Protocol):
    """Prompt 加载器抽象。

    业务模块通过 `get_prompt_loader()` 获取实例。
    当前唯一实现：`YAMLPromptLoader`（基于 YAML 文件 + mtime 缓存）。
    """

    def load(self, name: str) -> str:
        """按 name 加载 prompt 文本。

        Args:
            name: 形如 ``"intent"`` 或 ``"guard/chitchat"``，**不含扩展名**、**不含路径前缀**。
                仅允许：小写字母、数字、下划线、单层 ``/`` 分隔。

        Returns:
            prompt 原文（已 strip）。

        Raises:
            PromptNameError: name 不合法（路径越权或非法字符）。
            PromptNotFoundError: 文件不存在。
            PromptFormatError: YAML 解析失败 / content 字段缺失或为空。
        """
        ...


# =============================================================
# 实现：YAMLPromptLoader
# =============================================================
class YAMLPromptLoader:
    """基于 YAML 文件 + mtime 缓存的加载器。

    适用：单进程读多写少（V2 当前规模）。写并发留 V3+。
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()

    def load(self, name: str) -> str:
        # 1. name 校验（白名单正则）
        if not isinstance(name, str) or not _NAME_PATTERN.match(name):
            raise PromptNameError(f"非法 prompt name: {name!r}")

        # 2. 路径拼接 + resolve 后再次越权检查（防绕过正则的边界情况）
        rel_path = Path(f"{name}.yaml")
        full_path = (self._base_dir / rel_path).resolve()
        if not str(full_path).startswith(str(self._base_dir) + str(Path("/").resolve())):
            # 注：windows 上 resolved 路径可能大小写不同，allow case-insensitive
            full_str = str(full_path)
            base_str = str(self._base_dir)
            if not (full_str == base_str or full_str.startswith(base_str + "\\") or
                    full_str.startswith(base_str + "/")):
                raise PromptNameError(f"name 越权: {name!r}")

        # 3. 先检查文件存在性（fail-fast，比让 stat() 抛 FileNotFoundError 更友好）
        if not full_path.exists():
            raise PromptNotFoundError(f"prompt 不存在: {name}")

        # 4. mtime 缓存：未命中或 mtime 变化 → 重读
        mtime = full_path.stat().st_mtime
        with self._lock:
            cached = self._cache.get(name)
            if cached is not None and cached[0] == mtime:
                return cached[1]

            try:
                data = yaml.safe_load(full_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                raise PromptFormatError(name, f"YAML 解析失败: {e}") from e

            if isinstance(data, dict):
                content = data.get("content", "")
            else:
                content = data if isinstance(data, str) else ""

            if not isinstance(content, str) or not content.strip():
                raise PromptFormatError(name, "content 字段缺失或为空")

            content = content.strip()
            self._cache[name] = (mtime, content)
            return content


# =============================================================
# 工厂入口（依赖倒置 + 单例）
# =============================================================
def _resolve_base_dir() -> Path:
    """解析 prompts 基础目录。

    优先级：
    1. 环境变量 ``PROMPT_DIR`` 为绝对路径 → 直接使用
    2. 环境变量 ``PROMPT_DIR`` 为相对路径 → 相对 backend 根目录解析
    3. 默认：``backend/config/prompts``（相对 __file__ 三级父）

    这样无论用户 cwd 在哪，prompt 路径都稳定指向 backend 下。
    """
    from app.core.config import settings

    raw = Path(settings.PROMPT_DIR)
    if raw.is_absolute():
        return raw
    # prompt_loader.py 位于 backend/app/services/ → 三级父 = backend 根
    backend_root = Path(__file__).resolve().parents[2]
    return (backend_root / raw).resolve()


_loader: Optional[PromptLoader] = None
_loader_lock = threading.Lock()


def get_prompt_loader() -> PromptLoader:
    """工厂入口。业务模块**只能**通过此函数获取 loader（禁止直接 new）。

    Raises:
        RuntimeError: PROMPT_DIR 不存在（启动期 fail-fast，业务代码不需再处理）。
    """
    global _loader
    if _loader is not None:
        return _loader
    with _loader_lock:
        if _loader is not None:
            return _loader
        base = _resolve_base_dir()
        if not base.exists():
            raise RuntimeError(
                f"PROMPT_DIR 不存在: {base}。请创建目录或修正 settings.PROMPT_DIR。"
            )
        _loader = YAMLPromptLoader(base)
        return _loader


def reset_prompt_loader() -> None:
    """测试钩子：重置单例。生产代码**禁止**调用（仅供 test fixtures）。"""
    global _loader
    with _loader_lock:
        _loader = None
