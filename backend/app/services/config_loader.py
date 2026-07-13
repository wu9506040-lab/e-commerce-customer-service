"""
业务规则配置加载器 — Sprint 4 基础设施

业务模块通过 `get_config_loader().load(name)` 读取业务规则 YAML，
不再在业务代码中硬编码阈值/常量（CLAUDE.md §9.4.2）。

设计要点（roadmap §3.5 + CLAUDE.md §9.3.3）：
- Protocol 优先：业务依赖 `ConfigLoader` 抽象，不耦合实现
- name 白名单 + 路径 resolve 防越权（拒绝 `../` 注入）
- 启动时一次加载（不参与热更新：roadmap §3.5 明确）
- 单进程读多写少 → threading.Lock 保护 dict；写并发留 V3+

与 prompt_loader 的差异（设计边界）：
- 返回类型：dict（整个 YAML 顶层）vs prompt 的 str（YAML content 字段）
- 热更新：❌ 启动一次加载 vs prompt 的 ✅ mtime 失效
- name 模式：仅单层 `[a-z0-9_]+` vs prompt 的可分层 `xxx/yyy`
- 文件命名：直接 dict vs prompt 的 `content: |` 字段

不范围：
- 不做热更新 / mtime 失效（启动后规则不变；如需改 → 重启服务）
- 不做租户级覆盖（S6 范围）
- 不引入 Pydantic schema 校验（YAGNI：业务代码访问字段时自然抛 KeyError/TypeError）
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, runtime_checkable

import yaml


# =============================================================
# 自定义业务异常（就近定义，避免污染 core/）
# =============================================================
class ConfigError(Exception):
    """业务规则加载失败的基类异常。"""


class ConfigNameError(ConfigError):
    """业务规则 name 不合法（含路径分隔符、'..'、非法字符）。"""


class ConfigNotFoundError(ConfigError):
    """业务规则文件不存在。"""


class ConfigFormatError(ConfigError):
    """业务规则 YAML 解析失败或顶层非 dict。"""


# =============================================================
# Protocol（CLAUDE.md §9.3.3 — 业务模块靠此抽象，不直接 new）
# =============================================================
# 单层 name（无 / 分隔），与 prompt_loader 的可分层 pattern 区别
_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")


@runtime_checkable
class ConfigLoader(Protocol):
    """业务规则加载器抽象。

    业务模块通过 `get_config_loader()` 获取实例。
    当前唯一实现：`YAMLConfigLoader`（基于 YAML 文件 + 启动时缓存）。
    """

    def load(self, name: str) -> Dict[str, Any]:
        """按 name 加载业务规则 dict。

        Args:
            name: 形如 ``"guard"`` / ``"intent"``，**不含扩展名**、**不含路径前缀**。
                仅允许：小写字母、数字、下划线（**不**含 ``/``，与 prompt_loader 区别）。

        Returns:
            解析后的 dict（整个 YAML 顶层）。

        Raises:
            ConfigNameError: name 不合法（路径越权或非法字符）。
            ConfigNotFoundError: 文件不存在。
            ConfigFormatError: YAML 解析失败 / 顶层非 dict。
        """
        ...


# =============================================================
# 实现：YAMLConfigLoader
# =============================================================
class YAMLConfigLoader:
    """基于 YAML 文件 + 启动时缓存的加载器（不参与热更新）。

    适用：单进程读多写少（V2 当前规模）。写并发留 V3+。
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self, name: str) -> Dict[str, Any]:
        # 1. name 校验（白名单正则）
        if not isinstance(name, str) or not _NAME_PATTERN.match(name):
            raise ConfigNameError(f"非法业务规则 name: {name!r}")

        # 2. 路径拼接 + resolve 后再次越权检查（防绕过正则的边界情况）
        rel_path = Path(f"{name}.yaml")
        full_path = (self._base_dir / rel_path).resolve()
        base_str = str(self._base_dir)
        full_str = str(full_path)
        if not (full_str == base_str or full_str.startswith(base_str + "\\") or
                full_str.startswith(base_str + "/")):
            raise ConfigNameError(f"name 越权: {name!r}")

        # 3. 启动时一次加载：先看缓存，命中直接返回
        with self._lock:
            if name in self._cache:
                return self._cache[name]

            # 4. 文件存在性检查（fail-fast：明确错误优于 stat() 抛 FileNotFoundError）
            if not full_path.exists():
                raise ConfigNotFoundError(f"业务规则不存在: {name}")

            # 5. 解析 YAML
            try:
                data = yaml.safe_load(full_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                raise ConfigFormatError(f"YAML 解析失败 [{name}]: {e}") from e

            # 6. 顶层必须是 dict（业务规则一律用 dict 组织）
            if not isinstance(data, dict):
                raise ConfigFormatError(
                    f"业务规则 {name!r} 顶层必须是 dict，实际 {type(data).__name__}"
                )

            self._cache[name] = data
            return data


# =============================================================
# 工厂入口（依赖倒置 + 单例）
# =============================================================
def _resolve_base_dir() -> Path:
    """解析业务规则基础目录。

    优先级：
    1. 环境变量 ``BUSINESS_RULES_DIR`` 为绝对路径 → 直接使用
    2. 环境变量 ``BUSINESS_RULES_DIR`` 为相对路径 → 相对 backend 根目录解析
    3. 默认：``backend/config/business_rules``（相对 __file__ 三级父）

    这样无论用户 cwd 在哪，业务规则路径都稳定指向 backend 下。
    """
    from app.core.config import settings

    raw = Path(settings.BUSINESS_RULES_DIR)
    if raw.is_absolute():
        return raw
    # config_loader.py 位于 backend/app/services/ → 三级父 = backend 根
    backend_root = Path(__file__).resolve().parents[2]
    return (backend_root / raw).resolve()


_loader: Optional[ConfigLoader] = None
_loader_lock = threading.Lock()


def get_config_loader() -> ConfigLoader:
    """工厂入口。业务模块**只能**通过此函数获取 loader（禁止直接 new）。

    Raises:
        RuntimeError: BUSINESS_RULES_DIR 不存在（启动期 fail-fast，业务代码不需再处理）。
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
                f"BUSINESS_RULES_DIR 不存在: {base}。"
                f"请创建目录或修正 settings.BUSINESS_RULES_DIR。"
            )
        _loader = YAMLConfigLoader(base)
        return _loader


def reset_config_loader() -> None:
    """测试钩子：重置单例。生产代码**禁止**调用（仅供 test fixtures）。

    注：新 loader 重新创建时自带空缓存；旧 loader 实例会被 GC 回收，缓存随之消失。
    """
    global _loader
    with _loader_lock:
        _loader = None
