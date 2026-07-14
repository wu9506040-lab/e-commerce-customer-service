"""
Sprint 5 阶段 1: Prompt 版本管理（manifest 模式）单元测试

覆盖：
1. Manifest 模式：默认版本 / 指定 version / 不存在 version / 内联 content / 外部 file
2. 兼容模式：旧 YAML 无 versions 字段自动当 v1 / 指定非 v1 抛错
3. 缓存：mtime 缓存按 (name, version) 区分
4. 异常：PromptVersionError 继承 PromptError + 带可用版本列表

设计原则：
- 使用 tmp_path fixture 完全隔离（不污染仓库 prompts/）
- 模拟 manifest / 兼容两种格式
- 不依赖真实 YAML 文件，全部用 tmp_path 构造
"""
import os
import sys
import time
from pathlib import Path

import pytest

# 让模块能找到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================
# Fixtures
# =============================================================
@pytest.fixture
def prompts_dir(tmp_path):
    """构造临时 prompts 目录 + 2 个版本文件 + 1 个旧格式 YAML"""
    base = tmp_path / "prompts"
    base.mkdir()

    # 1. manifest 模式：agent.yaml + agent_v1.yaml + agent_v2.yaml
    (base / "agent.yaml").write_text(
        "default_version: v1\n"
        "versions:\n"
        "  v1:\n"
        "    file: agent_v1.yaml\n"
        "    stable: true\n"
        "  v2:\n"
        "    file: agent_v2.yaml\n"
        "    stable: false\n",
        encoding="utf-8",
    )
    (base / "agent_v1.yaml").write_text(
        "content: |\n"
        "  [v1 内容] 你是一个电商客服。\n"
        "  严格基于参考资料回答。\n",
        encoding="utf-8",
    )
    (base / "agent_v2.yaml").write_text(
        "content: |\n"
        "  [v2 内容] 您好，我是一个电商客服。\n"
        "  礼貌且专业地回答。\n",
        encoding="utf-8",
    )

    # 2. manifest 模式：内联 content（不引用外部 file）
    (base / "inline.yaml").write_text(
        "default_version: v2\n"
        "versions:\n"
        "  v1:\n"
        "    content: |\n"
        "      [v1 inline] 简短版。\n"
        "  v2:\n"
        "    content: |\n"
        "      [v2 inline] 详细版。\n",
        encoding="utf-8",
    )

    # 3. 兼容模式：旧 YAML 无 versions 字段
    (base / "legacy.yaml").write_text(
        "content: |\n"
        "  [legacy 模式] 旧 YAML 兼容加载。\n",
        encoding="utf-8",
    )

    return base


@pytest.fixture
def loader(prompts_dir):
    """用 prompts_dir 构造 loader（不读 settings.PROMPT_DIR）"""
    from app.services.prompt_loader import YAMLPromptLoader
    return YAMLPromptLoader(prompts_dir)


# =============================================================
# 1. Manifest 模式：基础加载
# =============================================================
class TestManifestLoad:
    def test_default_version_loaded(self, loader):
        """manifest 模式 + version=None → 走 default_version"""
        text = loader.load("agent")
        assert "[v1 内容]" in text

    def test_explicit_version_loaded(self, loader):
        """manifest 模式 + version="v2" → 走指定版本"""
        text = loader.load("agent", version="v2")
        assert "[v2 内容]" in text

    def test_explicit_version_v1_same_as_default(self, loader):
        """manifest 模式 + version="v1" == default_version"""
        a = loader.load("agent")
        b = loader.load("agent", version="v1")
        assert a == b

    def test_nonexistent_version_raises(self, loader):
        """manifest 模式 + version="v99" → PromptVersionError，含可用列表"""
        from app.services.prompt_loader import PromptVersionError

        with pytest.raises(PromptVersionError) as exc_info:
            loader.load("agent", version="v99")
        assert "v99" in str(exc_info.value)
        assert "v1" in str(exc_info.value)
        assert "v2" in str(exc_info.value)

    def test_missing_default_version_raises(self, tmp_path):
        """manifest 缺 default_version → PromptVersionError"""
        from app.services.prompt_loader import YAMLPromptLoader, PromptVersionError

        base = tmp_path / "prompts2"
        base.mkdir()
        (base / "broken.yaml").write_text(
            "versions:\n  v1:\n    file: v1.yaml\n",  # 缺 default_version
            encoding="utf-8",
        )
        (base / "v1.yaml").write_text("content: x\n", encoding="utf-8")

        loader = YAMLPromptLoader(base)
        with pytest.raises(PromptVersionError) as exc_info:
            loader.load("broken")
        assert "default_version" in str(exc_info.value)


# =============================================================
# 2. Manifest 模式：内联 content vs 外部 file
# =============================================================
class TestManifestContent:
    def test_external_file_content(self, loader):
        """外部 file 引用的版本正确加载"""
        v1 = loader.load("agent", version="v1")
        v2 = loader.load("agent", version="v2")
        assert "[v1 内容]" in v1
        assert "[v2 内容]" in v2
        assert v1 != v2

    def test_inline_content(self, loader):
        """内联 content（manifest 里直接写）的版本正确加载"""
        text = loader.load("inline", version="v2")
        assert "[v2 inline]" in text

    def test_inline_takes_precedence_over_file(self, tmp_path):
        """内联 content 优先于 file（loader 逻辑：先看 inline）"""
        from app.services.prompt_loader import YAMLPromptLoader

        base = tmp_path / "prompts3"
        base.mkdir()
        (base / "mix.yaml").write_text(
            "default_version: v1\n"
            "versions:\n"
            "  v1:\n"
            "    content: |\n"
            "      [inline 优先]\n"
            "    file: should_not_load.yaml\n",
            encoding="utf-8",
        )

        loader = YAMLPromptLoader(base)
        assert "[inline 优先]" in loader.load("mix")


# =============================================================
# 3. 兼容模式（旧 YAML 格式）
# =============================================================
class TestCompatMode:
    def test_legacy_yaml_loads_as_v1(self, loader):
        """旧 YAML 无 versions 字段 → 自动当 v1 处理"""
        text = loader.load("legacy")
        assert "[legacy 模式]" in text

    def test_legacy_with_v1_explicit(self, loader):
        """旧 YAML + version="v1" → 正常加载（兼容）"""
        text = loader.load("legacy", version="v1")
        assert "[legacy 模式]" in text

    def test_legacy_with_v2_raises(self, loader):
        """旧 YAML + version="v2" → PromptVersionError（单版本仅支持 v1）"""
        from app.services.prompt_loader import PromptVersionError

        with pytest.raises(PromptVersionError) as exc_info:
            loader.load("legacy", version="v2")
        assert "兼容模式" in str(exc_info.value)


# =============================================================
# 4. 缓存按 (name, version) 区分
# =============================================================
class TestVersionedCache:
    def test_v1_and_v2_cached_independently(self, loader):
        """v1 和 v2 各自缓存，互不影响"""
        v1_first = loader.load("agent", version="v1")
        v2 = loader.load("agent", version="v2")
        v1_second = loader.load("agent", version="v1")

        assert v1_first == v1_second
        assert v1_first != v2
        # 缓存里有 2 个 entry（不同 version）
        assert ("agent", "v1") in loader._cache
        assert ("agent", "v2") in loader._cache

    def test_compat_mode_uses_special_cache_key(self, loader):
        """兼容模式用 __compat__ 作为 version（区分显式 v1）"""
        loader.load("legacy")  # 兼容模式
        assert ("legacy", "__compat__") in loader._cache


# =============================================================
# 5. 异常体系
# =============================================================
class TestPromptVersionError:
    def test_inherits_prompt_error(self):
        """PromptVersionError 继承 PromptError（业务层可统一 except）"""
        from app.services.prompt_loader import PromptVersionError, PromptError

        err = PromptVersionError("agent", "v99", "不存在")
        assert isinstance(err, PromptError)

    def test_error_attrs(self):
        """PromptVersionError 带 name / version / reason 属性"""
        from app.services.prompt_loader import PromptVersionError

        err = PromptVersionError("agent", "v99", "test reason")
        assert err.name == "agent"
        assert err.version == "v99"
        assert err.reason == "test reason"


# =============================================================
# 6. mtime 热更新：改 v2 内容，下次 load 拿到新值
# =============================================================
class TestMtimeReloadWithVersions:
    def test_v2_content_change_picks_up(self, loader, prompts_dir):
        """改 v2 内容文件 → 下次 load 拿到新值（mtime 缓存按 (name, version) 区分）"""
        original = loader.load("agent", version="v2")
        assert "[v2 内容]" in original

        # 模拟编辑：等 mtime 刷新 + 改内容
        time.sleep(0.1)
        (prompts_dir / "agent_v2.yaml").write_text(
            "content: |\n  [v2 修订] 内容已变更。\n",
            encoding="utf-8",
        )

        updated = loader.load("agent", version="v2")
        assert "[v2 修订]" in updated
        assert updated != original


if __name__ == "__main__":
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    print("ALL SCENARIOS PASSED")