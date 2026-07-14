"""
Sprint 4 阶段 5: query_rewriter 业务规则集成测试

覆盖：
1. query_rewriter 启动期正确加载 query_rewriter.yaml（import 时一次加载）
2. 5 个业务字段（COREFERENCE_PATTERNS / MAX_HISTORY_TURNS / MAX_HISTORY_MSG_LEN / MAX_REWRITE_RATIO / MAX_REWRITE_EXTRA）与 YAML 一一对应
3. 行为一致性：rewrite_query 公共 API 行为不变（mock LLM）
4. query_rewriter.yaml 缺失 → import 阶段 ConfigError（fail-fast）

设计原则：
- 与 test_intent_config.py / test_refund_config.py 同模式（autouse + post 隔离）
- 行为测试覆盖 3 类（中文代词命中 / 英文未命中 / rewrite_query 走 LLM 路径）
"""
import os
import sys
from unittest.mock import patch

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
# 隔离 fixture：每次测试结束后重置 config_loader 单例
# 背景：与 test_refund_config / test_guard_config / test_intent_config 同模式
# =============================================================
@pytest.fixture(autouse=True)
def _isolate_config_loader_after():
    from app.services.config_loader import reset_config_loader

    yield
    reset_config_loader()


# =============================================================
# 1. query_rewriter 模块正确消费 YAML
# =============================================================
class TestQueryRewriterModuleLoadsYAML:
    """query_rewriter.py 在 import 阶段调用 get_config_loader().load('query_rewriter')，
    验证加载结果与模块导出的常量一致。"""

    def test_yaml_has_expected_fields(self):
        """YAML 文件含 5 个核心字段（防 YAML 字段被误删）。"""
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("query_rewriter")
        assert "COREFERENCE_PATTERNS" in yaml_data
        assert "MAX_HISTORY_TURNS" in yaml_data
        assert "MAX_HISTORY_MSG_LEN" in yaml_data
        assert "MAX_REWRITE_RATIO" in yaml_data
        assert "MAX_REWRITE_EXTRA" in yaml_data

    def test_yaml_coreference_patterns_count_preserved(self):
        """YAML 指代词数量与原硬编码完全一致（10 + 10 = 20 个）。"""
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("query_rewriter")
        assert len(yaml_data["COREFERENCE_PATTERNS"]) == 20

    def test_yaml_coreference_patterns_content_preserved(self):
        """YAML 指代词内容与原硬编码逐字一致（防偏移）。"""
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("query_rewriter")
        expected = [
            "它", "他们", "这个", "那个", "这些", "那些",
            "刚才", "之前", "上面", "下面",
            "那款", "这款", "这种", "那种", "前一个", "后一个", "前者", "后者",
            "这里", "那里",
        ]
        assert yaml_data["COREFERENCE_PATTERNS"] == expected

    def test_yaml_thresholds_values_preserved(self):
        """YAML 4 个阈值字段值与原硬编码完全一致（防修改时漏改）。"""
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("query_rewriter")
        assert yaml_data["MAX_HISTORY_TURNS"] == 4
        assert yaml_data["MAX_HISTORY_MSG_LEN"] == 100
        assert yaml_data["MAX_REWRITE_RATIO"] == 3
        assert yaml_data["MAX_REWRITE_EXTRA"] == 50

    def test_module_constants_match_yaml(self):
        """query_rewriter 模块常量与 YAML 字段一一对应（防偏移）。"""
        from app.services import query_rewriter
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("query_rewriter")
        assert query_rewriter.MAX_HISTORY_TURNS == yaml_data["MAX_HISTORY_TURNS"]
        assert query_rewriter.MAX_HISTORY_MSG_LEN == yaml_data["MAX_HISTORY_MSG_LEN"]
        assert query_rewriter.MAX_REWRITE_RATIO == yaml_data["MAX_REWRITE_RATIO"]
        assert query_rewriter.MAX_REWRITE_EXTRA == yaml_data["MAX_REWRITE_EXTRA"]

    def test_coreference_patterns_regex_compiled(self):
        """COREFERENCE_PATTERNS 是预编译的 re.Pattern，能匹配所有 20 个代词。"""
        from app.services import query_rewriter

        # 每个代词都应命中
        for word in [
            "它", "他们", "这个", "那个", "这些", "那些",
            "刚才", "之前", "上面", "下面",
            "那款", "这款", "这种", "那种", "前一个", "后一个", "前者", "后者",
            "这里", "那里",
        ]:
            assert query_rewriter.COREFERENCE_PATTERNS.search(word), (
                f"COREFERENCE_PATTERNS 应匹配 {word!r}"
            )

        # 嵌入句子中也应命中
        assert query_rewriter.COREFERENCE_PATTERNS.search("它多少钱")
        assert query_rewriter.COREFERENCE_PATTERNS.search("这款有货吗")
        assert query_rewriter.COREFERENCE_PATTERNS.search("我之前问的那个")
        # 不含代词的 query 不应命中
        assert not query_rewriter.COREFERENCE_PATTERNS.search("我要退款")
        assert not query_rewriter.COREFERENCE_PATTERNS.search("ZP1 多少钱")


# =============================================================
# 2. 公共 API 兼容性 + 行为一致性
# =============================================================
class TestQueryRewriterPublicAPI:
    """保证 4 个常量 + COREFERENCE_PATTERNS 公共符号 + rewrite_query 行为不变。"""

    def test_constants_are_int(self):
        """4 个阈值常量是 int（YAML 加载后类型校验）。"""
        from app.services import query_rewriter

        assert isinstance(query_rewriter.MAX_HISTORY_TURNS, int)
        assert isinstance(query_rewriter.MAX_HISTORY_MSG_LEN, int)
        assert isinstance(query_rewriter.MAX_REWRITE_RATIO, int)
        assert isinstance(query_rewriter.MAX_REWRITE_EXTRA, int)

    def test_multi_query_constants_loaded(self):
        """Phase 4 A4: 3 个 Multi-Query 配置常量加载（YAML 默认值）。"""
        from app.services import query_rewriter

        # 启期加载一次；类型 + 默认值校验
        assert isinstance(query_rewriter.ENABLE_MULTI_QUERY, bool)
        assert query_rewriter.ENABLE_MULTI_QUERY is False  # YAML 默认 false（灰度）
        assert isinstance(query_rewriter.MULTI_QUERY_COUNT, int)
        assert query_rewriter.MULTI_QUERY_COUNT == 3
        assert isinstance(query_rewriter.MULTI_QUERY_TRIGGER, str)
        assert query_rewriter.MULTI_QUERY_TRIGGER == "coref_only"

    def test_coreference_patterns_is_compiled(self):
        """COREFERENCE_PATTERNS 是预编译 re.Pattern（启动期一次编译）。"""
        import re as _re

        from app.services import query_rewriter

        assert isinstance(query_rewriter.COREFERENCE_PATTERNS, _re.Pattern)

    def test_rewrite_query_no_coreference_returns_original(self):
        """rewrite_query 无指代词 → 返回原 query, was_rewritten=False（L0 短路）。"""
        from app.services.query_rewriter import rewrite_query

        result, was_rewritten = rewrite_query("ZP1 多少钱", history=[{"role": "user", "content": "hi"}])
        assert result == "ZP1 多少钱"
        assert was_rewritten is False

    def test_rewrite_query_no_history_skips_llm(self):
        """rewrite_query 无 history → 返回原 query, was_rewritten=False（L1 短路，不调 LLM）。"""
        from app.services.query_rewriter import rewrite_query

        # 含指代词但无 history → 应跳过 LLM
        with patch("app.services.query_rewriter.get_llm_provider") as mock_provider:
            result, was_rewritten = rewrite_query("它多少钱", history=None)
            assert result == "它多少钱"
            assert was_rewritten is False
            # LLM 不应被调用
            mock_provider.assert_not_called()

    def test_rewrite_query_calls_llm_with_correct_messages(self):
        """rewrite_query 有指代词 + 有 history → 调 LLM，system + user 模板正确拼接。"""
        from app.services.query_rewriter import rewrite_query

        history = [
            {"role": "user", "content": "我想买 ZP1"},
            {"role": "assistant", "content": "ZP1 是新款"},
        ]

        with patch("app.services.query_rewriter.get_llm_provider") as mock_provider:
            mock_provider.return_value.chat.return_value = {"reply": "ZP1 多少钱"}
            result, was_rewritten = rewrite_query("它多少钱", history=history)

            assert result == "ZP1 多少钱"
            assert was_rewritten is True
            # 调 LLM 时 messages 应含 system + user
            call_args = mock_provider.return_value.chat.call_args
            messages = call_args[0][0]
            assert len(messages) == 2
            assert messages[0]["role"] == "system"
            assert messages[1]["role"] == "user"
            # user 模板含 {history} 和 {query} 占位符
            assert "ZP1 是新款" in messages[1]["content"]
            assert "它多少钱" in messages[1]["content"]


# =============================================================
# 3. fail-fast 行为
# =============================================================
class TestQueryRewriterFailFast:
    """query_rewriter.yaml 缺失 → import 阶段 ConfigError（fail-fast）。"""

    def test_missing_yaml_raises_at_import(self, monkeypatch, tmp_path):
        """BUSINESS_RULES_DIR 指向无 query_rewriter.yaml 的目录 → import query_rewriter 触发 ConfigError。"""
        from app.core import config
        from app.services.config_loader import ConfigError, reset_config_loader

        # 隔离：创建只有 guard.yaml 的目录（不放 query_rewriter.yaml）
        empty_dir = tmp_path / "empty_rules"
        empty_dir.mkdir()
        (empty_dir / "guard.yaml").write_text("MIN_LEN: 2\n", encoding="utf-8")
        monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", str(empty_dir))
        reset_config_loader()

        # import query_rewriter 应失败（找不到 query_rewriter.yaml）
        with pytest.raises(ConfigError, match="业务规则不存在: query_rewriter"):
            import importlib
            import app.services.query_rewriter
            importlib.reload(app.services.query_rewriter)