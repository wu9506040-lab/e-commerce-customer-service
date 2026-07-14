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


class PromptVersionError(PromptError):
    """prompt 版本不存在 / 不兼容（Sprint 5 manifest 模式）。"""

    def __init__(self, name: str, version: Optional[str], reason: str) -> None:
        super().__init__(f"prompt 版本错误 [{name}, version={version}]: {reason}")
        self.name = name
        self.version = version
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

    Sprint 5：扩展 `load(name, version=None)` 支持多版本 manifest。
    兼容模式：YAML 不含 `versions` 字段 → 自动当单版本 v1 处理。
    """

    def load(self, name: str, version: Optional[str] = None) -> str:
        """按 name 加载 prompt 文本。

        Args:
            name: 形如 ``"intent"`` 或 ``"guard/chitchat"``，**不含扩展名**、**不含路径前缀**。
                仅允许：小写字母、数字、下划线、单层 ``/`` 分隔。
            version: 版本号（manifest 模式生效），如 ``"v1"`` / ``"v2"``。
                - ``None``（默认）= 走 manifest.default_version
                - 兼容模式（YAML 无 versions 字段）= 强制 ``"v1"``，其他值抛错
                - manifest 模式但 version 不存在 = 抛 PromptVersionError

        Returns:
            prompt 原文（已 strip）。

        Raises:
            PromptNameError: name 不合法（路径越权或非法字符）。
            PromptNotFoundError: 文件不存在。
            PromptFormatError: YAML 解析失败 / content 字段缺失或为空。
            PromptVersionError: 指定 version 不存在 / 兼容模式指定非 v1。
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
        # Sprint 5：缓存 key 从 name 升级到 (name, version)；兼容模式 version="v1"
        self._cache: Dict[Tuple[str, str], Tuple[float, str]] = {}
        self._lock = threading.Lock()

    # =============================================================
    # Sprint 5：manifest 模式辅助方法
    # =============================================================
    @staticmethod
    def _is_manifest(data: object) -> bool:
        """判定 YAML 顶层是否为 manifest 格式（含 versions 字段）。"""
        return isinstance(data, dict) and "versions" in data

    @staticmethod
    def _resolve_version(manifest: dict, version: Optional[str]) -> str:
        """从 manifest 解析要加载的 version。

        规则：
        - version=None → 用 default_version
        - version 非 None → 直接用（调用方已确认）
        - version 不在 versions 字典中 → 抛 PromptVersionError

        Raises:
            PromptVersionError: default_version 缺失 / version 不存在
        """
        if version is not None:
            if version not in manifest["versions"]:
                available = ", ".join(sorted(manifest["versions"].keys()))
                raise PromptVersionError(
                    "<manifest>", version,
                    f"version 不存在，可用: {available}",
                )
            return version

        default = manifest.get("default_version")
        if not default:
            raise PromptVersionError(
                "<manifest>", None,
                "manifest 缺 default_version 字段",
            )
        if default not in manifest["versions"]:
            available = ", ".join(sorted(manifest["versions"].keys()))
            raise PromptVersionError(
                "<manifest>", default,
                f"default_version={default!r} 不在 versions 字典中，可用: {available}",
            )
        return default

    def _load_version_file_with_mtime(self, name: str, version: str, file_rel: str) -> Tuple[str, float]:
        """读 manifest 指定 version 的内容文件 + 返回文件 mtime（供 cache key 用）。

        Args:
            name: manifest 名称（用于报错）
            version: 版本号
            file_rel: 相对 base_dir 的 YAML 文件路径（如 ``agent_v1.yaml``）

        Returns:
            (prompt 文本已 strip, 文件 mtime)

        Raises:
            PromptNameError: file_rel 路径越权
            PromptNotFoundError: 内容文件不存在
            PromptFormatError: 内容文件格式错误
        """
        # file_rel 必须不含 ..，且 resolve 后必须在 base_dir 内
        if ".." in file_rel.split("/") or ".." in file_rel.split("\\"):
            raise PromptNameError(f"manifest version file 越权: {file_rel!r}")
        full_path = (self._base_dir / file_rel).resolve()
        base_str = str(self._base_dir)
        full_str = str(full_path)
        if not (full_str == base_str or full_str.startswith(base_str + "\\") or
                full_str.startswith(base_str + "/")):
            raise PromptNameError(f"manifest version file 越权: {file_rel!r}")

        if not full_path.exists():
            raise PromptNotFoundError(
                f"prompt version 内容文件不存在 [{name}, version={version}]: {file_rel}"
            )

        file_mtime = full_path.stat().st_mtime

        try:
            data = yaml.safe_load(full_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise PromptFormatError(
                f"{name}/{version}", f"YAML 解析失败: {e}"
            ) from e

        if isinstance(data, dict):
            content = data.get("content", "")
        else:
            content = data if isinstance(data, str) else ""

        if not isinstance(content, str) or not content.strip():
            raise PromptFormatError(
                f"{name}/{version}", "content 字段缺失或为空"
            )

        return content.strip(), file_mtime

    # =============================================================
    # 主入口
    # =============================================================
    def load(self, name: str, version: Optional[str] = None) -> str:
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
        #    Sprint 5：缓存 key 是 (name, version)，version 默认 "v1"（兼容模式）
        cache_version = version if version is not None else "__compat__"
        cache_key = (name, cache_version)
        mtime = full_path.stat().st_mtime
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[0] == mtime:
                return cached[1]

            try:
                data = yaml.safe_load(full_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                raise PromptFormatError(name, f"YAML 解析失败: {e}") from e

            # Sprint 5：manifest 模式 vs 兼容模式分流
            if self._is_manifest(data):
                manifest = data
                actual_version = self._resolve_version(manifest, version)
                ver_info = manifest["versions"][actual_version]

                # 1) 内联 content（manifest 里直接写）
                inline_content = ver_info.get("content")
                if isinstance(inline_content, str) and inline_content.strip():
                    content = inline_content.strip()
                else:
                    # 2) 引用外部 file
                    file_rel = ver_info.get("file")
                    if not file_rel or not isinstance(file_rel, str):
                        raise PromptFormatError(
                            f"{name}/{actual_version}",
                            "version 缺 file 或 content 字段",
                        )
                    content, version_file_mtime = self._load_version_file_with_mtime(
                        name, actual_version, file_rel
                    )
                    # 内容文件被改 → manifest mtime 不变也会触发重读
                    mtime = max(mtime, version_file_mtime)
            else:
                # 兼容模式：YAML 无 versions 字段 → 当单版本 v1
                if version is not None and version != "v1":
                    raise PromptVersionError(
                        name, version,
                        "YAML 不含 versions 字段（兼容模式仅支持 v1）",
                    )
                if isinstance(data, dict):
                    content = data.get("content", "")
                else:
                    content = data if isinstance(data, str) else ""

                if not isinstance(content, str) or not content.strip():
                    raise PromptFormatError(name, "content 字段缺失或为空")
                content = content.strip()

            self._cache[cache_key] = (mtime, content)
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
