"""
Sprint 4: 业务规则加载器单元测试

覆盖：
1. YAMLConfigLoader：基本加载 / 缓存 / 错误处理 / 路径安全
2. ConfigLoader Protocol：runtime_checkable + 鸭子类型
3. 自定义异常：继承关系清晰（业务层可统一 except ConfigError）
4. get_config_loader()：单例 + 启动期 fail-fast + env 覆盖

设计原则：
- 使用 pytest 内置 tmp_path fixture 做文件隔离（不污染仓库）
- 不依赖外部服务（PyYAML 是 Sprint 2 唯一新增依赖）
- settings.BUSINESS_RULES_DIR 通过 monkeypatch 注入 test 路径（不动 default）
"""
import os
import sys
from pathlib import Path

import pytest

# 让模块能找到 app 包（与项目其他测试一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 触发工厂路径解析会 import app.core.config；提前 set 假值避免 _validate_jwt_secret 抛错
os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# =============================================================
# Fixtures
# =============================================================
@pytest.fixture
def rules_dir(tmp_path):
    """隔离的业务规则目录（每次测试独立）。"""
    p = tmp_path / "business_rules"
    p.mkdir()
    return p


@pytest.fixture
def write_yaml(rules_dir):
    """factory: write_yaml(name, content) 写 {rules_dir}/{name}.yaml。"""
    def _write(name: str, body: str):
        target = rules_dir / f"{name}.yaml"
        target.write_text(body, encoding="utf-8")
        return target
    return _write


@pytest.fixture
def loader(rules_dir):
    """YAMLConfigLoader 直接构造（绕过工厂，专注 loader 行为测试）。"""
    from app.services.config_loader import YAMLConfigLoader
    return YAMLConfigLoader(rules_dir)


@pytest.fixture(autouse=True)
def reset_singleton():
    """每次测试前后重置工厂单例（防止跨测试污染）。"""
    from app.services.config_loader import reset_config_loader
    reset_config_loader()
    yield
    reset_config_loader()


# =============================================================
# 1. YAMLConfigLoader 行为
# =============================================================
class TestYAMLConfigLoader:
    """YAMLConfigLoader 核心行为：加载 / 缓存 / 错误处理 / 路径安全。"""

    def test_load_returns_dict_for_valid_yaml(self, loader, write_yaml):
        """基本路径：读取 YAML 顶层 dict。"""
        write_yaml("guard", "MIN_LEN: 2\nMAX_LEN: 500\n")
        result = loader.load("guard")
        assert result == {"MIN_LEN": 2, "MAX_LEN": 500}

    def test_load_caches_result(self, loader, write_yaml):
        """第二次调用走缓存（启动时一次加载）。"""
        write_yaml("guard", "MIN_LEN: 2\n")
        # 第一次：加载
        result1 = loader.load("guard")
        assert result1 == {"MIN_LEN": 2}
        # 第二次：走缓存（修改文件不影响返回值）
        write_yaml("guard", "MIN_LEN: 999\n")
        result2 = loader.load("guard")
        assert result2 == {"MIN_LEN": 2}  # 仍是旧值，证明缓存生效

    def test_load_missing_file_raises_not_found(self, loader):
        """文件不存在 → ConfigNotFoundError。"""
        from app.services.config_loader import ConfigNotFoundError
        with pytest.raises(ConfigNotFoundError, match="业务规则不存在: missing"):
            loader.load("missing")

    def test_load_invalid_name_raises_name_error(self, loader):
        """非法 name（含空 / / \\ ..）→ ConfigNameError。"""
        from app.services.config_loader import ConfigNameError
        for bad_name in ["", "../etc/passwd", "guard/centroid", "guard\\centroid", "Guard", "guard.yml"]:
            with pytest.raises(ConfigNameError, match="非法业务规则 name"):
                loader.load(bad_name)

    def test_load_top_level_not_dict_raises_format_error(self, loader, write_yaml):
        """顶层非 dict（list / str / int）→ ConfigFormatError。"""
        from app.services.config_loader import ConfigFormatError
        # 顶层是 list
        write_yaml("bad_list", "- 1\n- 2\n")
        with pytest.raises(ConfigFormatError, match="顶层必须是 dict"):
            loader.load("bad_list")
        # 顶层是 str
        write_yaml("bad_str", "hello\n")
        with pytest.raises(ConfigFormatError, match="顶层必须是 dict"):
            loader.load("bad_str")

    def test_load_invalid_yaml_raises_format_error(self, loader, write_yaml):
        """YAML 解析失败 → ConfigFormatError。"""
        from app.services.config_loader import ConfigFormatError
        # 用不闭合的引号触发 YAML 解析错
        write_yaml("bad_yaml", "MIN_LEN: 'unclosed\n")
        with pytest.raises(ConfigFormatError, match="YAML 解析失败"):
            loader.load("bad_yaml")

    def test_load_path_traversal_blocked_by_resolve(self, loader, write_yaml, tmp_path):
        """路径越权（../）即使绕过正则也会被 resolve 拦截。"""
        from app.services.config_loader import ConfigNameError
        # 写一个 ../etc/passwd.yaml 在 tmp_path 外
        (tmp_path / "passwd.yaml").write_text("MALICIOUS: true\n", encoding="utf-8")
        # name 含 ".."，正则已拦；这里直接验证 ConfigNameError
        with pytest.raises(ConfigNameError):
            loader.load("../passwd")


# =============================================================
# 2. 异常体系
# =============================================================
class TestConfigExceptions:
    """异常继承关系：业务层可统一 except ConfigError。"""

    def test_all_inherit_config_error(self):
        from app.services.config_loader import (
            ConfigError,
            ConfigNameError,
            ConfigNotFoundError,
            ConfigFormatError,
        )
        assert issubclass(ConfigNameError, ConfigError)
        assert issubclass(ConfigNotFoundError, ConfigError)
        assert issubclass(ConfigFormatError, ConfigError)

    def test_unified_catch_via_config_error(self, loader, write_yaml):
        """业务层可以 except ConfigError 统一捕获所有加载失败。"""
        from app.services.config_loader import ConfigError
        write_yaml("bad", "- 1\n")  # 顶层非 dict
        with pytest.raises(ConfigError):
            loader.load("bad")


# =============================================================
# 3. Protocol 鸭子类型
# =============================================================
class TestConfigLoaderProtocol:
    """ConfigLoader Protocol：runtime_checkable + 鸭子类型。"""

    def test_protocol_is_runtime_checkable(self):
        from app.services.config_loader import ConfigLoader, YAMLConfigLoader
        from pathlib import Path
        loader = YAMLConfigLoader(Path("/tmp"))
        assert isinstance(loader, ConfigLoader)

    def test_yaml_loader_satisfies_protocol(self):
        """鸭子类型：任何有 load(name) -> dict 属性的对象都满足 Protocol。"""
        from app.services.config_loader import ConfigLoader

        class DuckLoader:
            def load(self, name: str) -> dict:
                return {"MIN_LEN": 42}

        assert isinstance(DuckLoader(), ConfigLoader)


# =============================================================
# 4. 工厂入口
# =============================================================
class TestGetConfigLoaderFactory:
    """get_config_loader()：单例 + 启动期 fail-fast + env 覆盖。"""

    def test_factory_returns_singleton(self, monkeypatch, rules_dir):
        """多次调用返回同一实例。"""
        from app.services.config_loader import get_config_loader
        # 用 monkeypatch 临时改 settings.BUSINESS_RULES_DIR
        from app.core import config
        monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", str(rules_dir))
        loader1 = get_config_loader()
        loader2 = get_config_loader()
        assert loader1 is loader2

    def test_factory_dir_not_exists_raises_runtime_error(self, monkeypatch):
        """BUSINESS_RULES_DIR 不存在 → RuntimeError（启动期 fail-fast）。"""
        from app.services.config_loader import get_config_loader
        from app.core import config
        monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", "/nonexistent/path/xyz")
        with pytest.raises(RuntimeError, match="BUSINESS_RULES_DIR 不存在"):
            get_config_loader()

    def test_factory_absolute_path_overrides(self, monkeypatch, rules_dir):
        """绝对路径直接使用，不解析为相对。"""
        from app.services.config_loader import get_config_loader
        from app.core import config
        # 写一个示例 rule
        (rules_dir / "guard.yaml").write_text("MIN_LEN: 2\n", encoding="utf-8")
        monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", str(rules_dir))
        loader = get_config_loader()
        result = loader.load("guard")
        assert result == {"MIN_LEN": 2}

    def test_factory_relative_path_resolved_against_backend_root(self, monkeypatch, tmp_path):
        """相对路径解析：相对 backend 根目录（__file__ 三级父）。"""
        from app.services.config_loader import get_config_loader, _resolve_base_dir
        from app.core import config
        # 模拟 backend 根下的 config/business_rules
        backend_root = Path(__file__).resolve().parents[1]  # tests 的父目录 = backend 根
        rel_dir = backend_root / "config" / "business_rules"
        if not rel_dir.exists():
            pytest.skip("backend/config/business_rules 目录不存在（未建 .gitkeep）")
        monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", "config/business_rules")
        resolved = _resolve_base_dir()
        # 解析后应是绝对路径
        assert resolved.is_absolute()
        # 用 Path.parts 跨平台比较（Windows 用 \，Linux 用 /）
        assert Path(resolved).parts[-2:] == ("config", "business_rules")
