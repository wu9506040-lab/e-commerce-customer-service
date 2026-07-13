"""
Sprint 4 阶段 4: intent_service 业务规则集成测试

覆盖：
1. intent_service 启动期正确加载 intent.yaml（import 时一次加载）
2. 3 个业务规则（INTENT_RULES / ORDER_NO_RE / SKU_RE）与 YAML 一一对应（防偏移）
3. 行为一致性：5 条 critical pattern 仍命中原意图
4. intent.yaml 缺失 → import 阶段 RuntimeError（fail-fast）

设计原则：
- 与 test_refund_config.py / test_guard_config.py 同模式（autouse + post 隔离）
- 行为测试只覆盖 5 条 critical pattern；完整 81 条由「pattern 数量 = 81 + 4 intents 完整」间接保证
"""
import os
import sys

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
# 背景：与 test_refund_config / test_guard_config 同模式 — fail-fast 测试 reload 模块时
#       get_config_loader() 会在 load() 抛错前把 _loader 指向污染目录 → 必须在文件末尾 reset。
# =============================================================
@pytest.fixture(autouse=True)
def _isolate_config_loader_after():
    from app.services.config_loader import reset_config_loader

    yield
    reset_config_loader()


# =============================================================
# 1. intent_service 模块正确消费 YAML
# =============================================================
class TestIntentModuleLoadsYAML:
    """intent_service.py 在 import 阶段调用 get_config_loader().load('intent')，
    验证加载结果与模块导出的常量一致。"""

    def test_intent_yaml_has_expected_fields(self):
        """YAML 文件含 5 个核心字段（防 YAML 字段被误删）。"""
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("intent")
        assert "INTENT_RULES" in yaml_data
        assert "ORDER_NO_RE_PATTERN" in yaml_data
        assert "ORDER_NO_RE_FLAGS" in yaml_data
        assert "SKU_RE_PATTERN" in yaml_data
        assert "SKU_RE_FLAGS" in yaml_data

    def test_intent_yaml_has_all_4_intents(self):
        """YAML 含 4 类意图（refund / policy / order / product）。"""
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("intent")
        yaml_intents = set(yaml_data["INTENT_RULES"].keys())
        assert yaml_intents == {"refund_query", "policy_query", "order_query", "product_query"}

    def test_intent_yaml_pattern_counts_preserved(self):
        """YAML pattern 数量与原硬编码完全一致（21 + 30 + 12 + 18 = 81）。"""
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("intent")
        yaml_counts = {k: len(v) for k, v in yaml_data["INTENT_RULES"].items()}
        assert yaml_counts["refund_query"] == 21
        assert yaml_counts["policy_query"] == 30
        assert yaml_counts["order_query"] == 12
        assert yaml_counts["product_query"] == 18
        assert sum(yaml_counts.values()) == 81

    def test_intent_rules_constants_match_yaml(self):
        """intent_service.INTENT_RULES 与 YAML 字段一一对应（防偏移）。"""
        from app.services import intent_service
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("intent")
        # 4 intents 完整
        assert set(intent_service.INTENT_RULES.keys()) == {
            "refund_query", "policy_query", "order_query", "product_query",
        }
        # 每条 intent 的 pattern 数量与 YAML 一致
        for intent, patterns in intent_service.INTENT_RULES.items():
            assert len(patterns) == len(yaml_data["INTENT_RULES"][intent])

    def test_intent_rules_order_preserved(self):
        """INTENT_RULES 顺序敏感：refund_query → policy_query → order_query → product_query。
        与原 tuple 列表顺序完全一致；dict 保序（Python 3.7+）。"""
        from app.services import intent_service

        order = list(intent_service.INTENT_RULES.keys())
        assert order == ["refund_query", "policy_query", "order_query", "product_query"]

    def test_order_no_re_and_sku_re_match_yaml(self):
        """ORDER_NO_RE / SKU_RE 的 pattern + flags 与 YAML 一致。"""
        from app.services import intent_service
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("intent")
        assert intent_service.ORDER_NO_RE.pattern == yaml_data["ORDER_NO_RE_PATTERN"]
        assert intent_service.SKU_RE.pattern == yaml_data["SKU_RE_PATTERN"]
        # flags 用 getattr(re, name) 转换后应含 re.IGNORECASE
        # 注：re.compile 的 flags 值含默认位（如 UNICODE=32），故用按位与判断而非 ==
        import re as _re
        assert intent_service.ORDER_NO_RE.flags & _re.IGNORECASE
        assert intent_service.SKU_RE.flags & _re.IGNORECASE


# =============================================================
# 2. 公共 API 兼容性 + 行为一致性
# =============================================================
class TestIntentPublicAPI:
    """保证 INTENT_RULES / ORDER_NO_RE / SKU_RE 公共符号 + IntentService.classify 行为不变。"""

    def test_intent_rules_public_dict_accessible(self):
        """INTENT_RULES 是 dict[str, list[str]]，公共可访问。"""
        from app.services.intent_service import INTENT_RULES

        assert isinstance(INTENT_RULES, dict)
        assert all(isinstance(k, str) for k in INTENT_RULES.keys())
        assert all(isinstance(v, list) for v in INTENT_RULES.values())

    def test_order_no_re_matches_m13_format(self):
        """ORDER_NO_RE 能匹配字母后缀订单号（如 ORD20260704899EBA）。M13 修复特性。"""
        from app.services.intent_service import ORDER_NO_RE

        m = ORDER_NO_RE.search("ORD20260704899EBA")
        assert m is not None
        assert m.group(0) == "ORD20260704899EBA"

    def test_sku_re_matches_zp_bp_lp_prefix(self):
        """SKU_RE 匹配 ZP / BP / LP 前缀 + 1-3 位数字。"""
        from app.services.intent_service import SKU_RE

        for sku in ["ZP1", "ZP123", "BP42", "LP9"]:
            m = SKU_RE.search(sku)
            assert m is not None, f"SKU_RE should match {sku}"
            assert m.group(0) == sku

    def test_classify_behavior_preserved_refund(self):
        """classify('我要退款') → refund_query（行为一致性）。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我要退款")
        assert result["intent"] == "refund_query"
        assert result["method"] == "rule"
        assert result["confidence"] == 1.0

    def test_classify_behavior_preserved_policy(self):
        """classify('7 天无理由退货运费谁出') → policy_query（询问流程 → policy，非 refund）。
        关键回归：refund_query 在前可能误命中 → 验证顺序敏感逻辑正确。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("7 天无理由退货运费谁出")
        assert result["intent"] == "policy_query"
        assert result["method"] == "rule"

    def test_classify_behavior_preserved_product(self):
        """classify('ZP1 现在有货吗') → product_query（SKU 优先）。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("ZP1 现在有货吗")
        assert result["intent"] == "product_query"

    def test_classify_extracts_order_no(self):
        """classify 抽取订单号（M13 字母后缀订单）。"""
        from app.services.intent_service import IntentService

        result = IntentService.classify("我的订单 ORD20260704899EBA 到哪了")
        entities = result["entities"]
        assert entities["order_no"] == "ORD20260704899EBA"


# =============================================================
# 3. fail-fast 行为
# =============================================================
class TestIntentFailFast:
    """intent.yaml 缺失 → import 阶段 RuntimeError（fail-fast）。"""

    def test_missing_intent_yaml_raises_at_import(self, monkeypatch, tmp_path):
        """BUSINESS_RULES_DIR 指向无 intent.yaml 的目录 → import intent_service 触发 ConfigError。"""
        from app.core import config
        from app.services.config_loader import ConfigError, reset_config_loader

        # 隔离：创建只有 guard.yaml 的目录（不放 intent.yaml）
        empty_dir = tmp_path / "empty_rules"
        empty_dir.mkdir()
        (empty_dir / "guard.yaml").write_text("MIN_LEN: 2\n", encoding="utf-8")
        monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", str(empty_dir))
        reset_config_loader()

        # import intent_service 应失败（找不到 intent.yaml）
        with pytest.raises(ConfigError, match="业务规则不存在: intent"):
            import importlib
            import app.services.intent_service
            importlib.reload(app.services.intent_service)