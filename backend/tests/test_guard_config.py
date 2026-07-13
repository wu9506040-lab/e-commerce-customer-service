"""
Sprint 4: guard.py 业务规则集成测试

覆盖：
1. guard 模块启动期正确加载 guard.yaml（import 时一次加载）
2. 7 个阈值常量 + 6 条话术与 YAML 一一对应（防止偏移）
3. guard.yaml 缺失时 → import guard 阶段 RuntimeError（fail-fast）

设计原则：
- 与 test_config_loader.py 互补：本文件测「guard 业务侧正确消费 YAML」
- 不重复 test_config_loader.py 已覆盖的「loader 自身行为」（加载/缓存/路径安全）
- 使用真实的 backend/config/business_rules/guard.yaml（不污染仓库）
"""
import os
import sys
from pathlib import Path

import pytest

# 让模块能找到 app 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 触发工厂路径解析会 import app.core.config；提前 set 假值避免 _validate_jwt_secret 抛错
os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# =============================================================
# 1. guard 模块正确消费 YAML
# =============================================================
class TestGuardModuleLoadsYAML:
    """guard.py 在 import 阶段调用 get_config_loader().load('guard')，
    验证加载结果与模块导出的常量一致。"""

    def test_guard_constants_match_yaml(self):
        """7 个阈值常量与 guard.yaml 字段一一对应（防止 YAML 改名但 guard 没改）。"""
        from app.services import guard
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("guard")
        # L1
        assert guard.MIN_LEN == yaml_data["MIN_LEN"] == 2
        assert guard.MAX_LEN == yaml_data["MAX_LEN"] == 500
        assert guard.MIN_CHAR_DIVERSITY == yaml_data["MIN_CHAR_DIVERSITY"] == 0.15
        assert guard.MIN_CHINESE_RATIO == yaml_data["MIN_CHINESE_RATIO"] == 0.20
        # L2
        assert guard.DOMAIN_RELEVANCE_THRESHOLD == yaml_data["DOMAIN_RELEVANCE_THRESHOLD"] == 0.55
        # L3
        assert guard.REPEAT_WINDOW_SECONDS == yaml_data["REPEAT_WINDOW_SECONDS"] == 60
        assert guard.REPEAT_MAX_IN_WINDOW == yaml_data["REPEAT_MAX_IN_WINDOW"] == 3

    def test_guard_chitchat_responses_match_yaml(self):
        """CHITCHAT_RESPONSES dict 6 条话术与 YAML 一一对应。"""
        from app.services import guard
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("guard")
        yaml_chitchat = yaml_data["CHITCHAT_RESPONSES"]

        # 6 个 key 完整
        expected_keys = {"no_service", "irrelevant", "too_short", "too_long", "spam", "english_no_sku"}
        assert set(guard.CHITCHAT_RESPONSES.keys()) == expected_keys
        assert set(yaml_chitchat.keys()) == expected_keys
        # 每条话术完全一致（防止 YAML 改了忘同步 guard）
        for k in expected_keys:
            assert guard.CHITCHAT_RESPONSES[k] == yaml_chitchat[k]
            assert isinstance(guard.CHITCHAT_RESPONSES[k], str)
            assert len(guard.CHITCHAT_RESPONSES[k]) > 0

    def test_guard_chitchat_is_same_object_as_yaml_dict(self):
        """CHITCHAT_RESPONSES 引用同一个 dict 对象（避免每次访问重新加载）。"""
        from app.services import guard
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("guard")
        # 同一对象引用（启动期一次加载 + 赋值）
        assert guard.CHITCHAT_RESPONSES is yaml_data["CHITCHAT_RESPONSES"]


# =============================================================
# 2. guard 调用方兼容性（保护对外接口）
# =============================================================
class TestGuardPublicAPI:
    """保证 guard 公开的常量名 + 调用方式不变（chat.py 依赖）。"""

    def test_public_constant_names_exported(self):
        """所有原硬编码常量名仍可访问（保护调用方代码不动）。"""
        from app.services.guard import (
            MIN_LEN,
            MAX_LEN,
            MIN_CHAR_DIVERSITY,
            MIN_CHINESE_RATIO,
            DOMAIN_RELEVANCE_THRESHOLD,
            REPEAT_WINDOW_SECONDS,
            REPEAT_MAX_IN_WINDOW,
            CHITCHAT_RESPONSES,
        )
        # 所有名字都能 import，类型符合预期
        assert isinstance(MIN_LEN, int)
        assert isinstance(MAX_LEN, int)
        assert isinstance(MIN_CHAR_DIVERSITY, float)
        assert isinstance(MIN_CHINESE_RATIO, float)
        assert isinstance(DOMAIN_RELEVANCE_THRESHOLD, float)
        assert isinstance(REPEAT_WINDOW_SECONDS, int)
        assert isinstance(REPEAT_MAX_IN_WINDOW, int)
        assert isinstance(CHITCHAT_RESPONSES, dict)

    def test_inputguard_singleton_still_works(self):
        """InputGuard 单例 `guard` 仍可调用 .check()。"""
        from app.services.guard import guard, GuardResult

        # 简单 L1 触发：长度 < MIN_LEN
        result = guard.check("a", user_id=0)
        assert isinstance(result, GuardResult)
        assert result.allowed is False
        assert result.reason == "too_short"
        assert result.layer == "L1"
        # response 不为空（来自 YAML 话术）
        assert result.response is not None
        assert len(result.response) > 0


# =============================================================
# 3. fail-fast 行为（YAML 缺失 → import 阶段崩）
# =============================================================
class TestGuardFailFast:
    """guard.yaml 缺失 → import guard 阶段 RuntimeError（不允许运行时默认）。

    设计意图：启动期暴露问题，比运行时 silent default 更安全。
    """

    def test_missing_guard_yaml_raises_at_import(self, monkeypatch, tmp_path):
        """BUSINESS_RULES_DIR 指向空目录 → import app.services.guard 触发 ConfigNotFoundError。

        设计意图：启动期暴露问题，比运行时 silent default 更安全。
        注：guard.yaml 缺失抛 ConfigNotFoundError；BUSINESS_RULES_DIR 本身缺失才抛 RuntimeError。
            业务层统一 except ConfigError 即可（详见 test_config_loader）。
        """
        from app.core import config
        from app.services.config_loader import ConfigError

        # 隔离：tmp_path 下的空目录作为 BUSINESS_RULES_DIR
        empty_dir = tmp_path / "empty_rules"
        empty_dir.mkdir()
        monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", str(empty_dir))

        # 重置 loader 单例，使新的 BUSINESS_RULES_DIR 生效
        from app.services.config_loader import reset_config_loader
        reset_config_loader()

        # 关键：import 时会执行模块顶部 _RULES = get_config_loader().load("guard")
        with pytest.raises(ConfigError, match="业务规则不存在: guard"):
            # 重新 import 以触发加载
            import importlib
            import app.services.guard
            importlib.reload(app.services.guard)