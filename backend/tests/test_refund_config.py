"""
Sprint 4: refund_graph / refund_tool / order_lifecycle 业务规则集成测试

覆盖：
1. 3 个文件启动期正确加载 refund.yaml（import 时一次加载）
2. 3 文件常量值与 YAML 一一对应（单一真相源 — 防偏移）
3. refund.yaml 缺失 → import 阶段 RuntimeError（fail-fast）

设计原则：
- 与 test_config_loader.py 互补：本文件测「3 个业务侧文件正确消费同一份 YAML」
- 与 test_guard_config.py 同模式（同名引用透明替换）
- 关键差异：refund 常量是「跨模块共享」，必须验证 3 文件值一致
  （迁移前是 3 处硬编码同一数值，迁移后必须仍然是 3 处共享同一份 YAML）
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
# 背景：TestRefundFailFast 通过 monkeypatch 改 BUSINESS_RULES_DIR 并 reload refund_graph；
#       reload 过程中 get_config_loader() 会创建指向 monkeypatch 后目录的 loader，
#       pytest.raises 结束后 monkeypatch 恢复 settings，但 _loader 全局仍指向污染目录。
#       必须在文件末尾强制重置 _loader = None，否则污染下游 test。
# 注意：只用 post-reset（不 pre），避免破坏测试期间 loader 缓存的稳定性；
#       test_config_loader.py 的 pre+post 模式在 fail-fast 测试在前时会破坏同模块的"is"断言。
# =============================================================
@pytest.fixture(autouse=True)
def _isolate_config_loader_after():
    from app.services.config_loader import reset_config_loader

    yield
    reset_config_loader()


# =============================================================
# 1. 3 文件常量与 YAML 同步（单一真相源）
# =============================================================
class TestRefundModuleLoadsYAML:
    """refund_graph / refund_tool / order_lifecycle 3 个文件 import 时
    正确加载同一份 refund.yaml，且常量值与 YAML 一一对应。"""

    def test_refund_yaml_has_expected_fields(self):
        """YAML 文件含 2 个核心字段（防 YAML 字段被误删）。"""
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("refund")
        assert "REFUND_WINDOW_DAYS" in yaml_data
        assert "DELIVERY_OFFSET_DAYS" in yaml_data
        assert yaml_data["REFUND_WINDOW_DAYS"] == 7
        assert yaml_data["DELIVERY_OFFSET_DAYS"] == 2

    def test_refund_graph_constants_match_yaml(self):
        """refund_graph.py 顶部 REFUND_WINDOW_DAYS / DELIVERY_OFFSET_DAYS = YAML 值。"""
        from app.services import refund_graph
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("refund")
        assert refund_graph.REFUND_WINDOW_DAYS == yaml_data["REFUND_WINDOW_DAYS"] == 7
        assert refund_graph.DELIVERY_OFFSET_DAYS == yaml_data["DELIVERY_OFFSET_DAYS"] == 2

    def test_refund_tool_class_attribute_matches_yaml(self):
        """RefundTool.REFUND_WINDOW_DAYS 类属性 = YAML 值（保持类属性语法兼容）。"""
        from app.services.config_loader import get_config_loader
        from app.tools.refund_tool import RefundTool

        yaml_data = get_config_loader().load("refund")
        assert RefundTool.REFUND_WINDOW_DAYS == yaml_data["REFUND_WINDOW_DAYS"] == 7
        # 类型保持 int（迁移前是 int）
        assert isinstance(RefundTool.REFUND_WINDOW_DAYS, int)

    def test_order_lifecycle_module_constant_matches_yaml(self):
        """order_lifecycle.DELIVERY_OFFSET_DAYS 模块级常量 = YAML 值。"""
        from app.services import order_lifecycle
        from app.services.config_loader import get_config_loader

        yaml_data = get_config_loader().load("refund")
        assert order_lifecycle.DELIVERY_OFFSET_DAYS == yaml_data["DELIVERY_OFFSET_DAYS"] == 2
        assert isinstance(order_lifecycle.DELIVERY_OFFSET_DAYS, int)

    def test_three_files_share_single_source_of_truth(self):
        """核心断言：3 文件值必须全部相等（防止任何一处「跑偏」）。

        迁移前是 3 处硬编码同一数值，迁移后必须是 3 处共享同一份 YAML。
        如果任一文件未来改了 hard-coded 值或指向其他源，这个测试会失败。
        """
        from app.services import refund_graph, order_lifecycle
        from app.tools.refund_tool import RefundTool

        # REFUND_WINDOW_DAYS 在 refund_graph + RefundTool 两处
        assert refund_graph.REFUND_WINDOW_DAYS == RefundTool.REFUND_WINDOW_DAYS == 7
        # DELIVERY_OFFSET_DAYS 在 refund_graph + order_lifecycle 两处
        assert refund_graph.DELIVERY_OFFSET_DAYS == order_lifecycle.DELIVERY_OFFSET_DAYS == 2


# =============================================================
# 2. 公共 API 兼容性（保护调用方）
# =============================================================
class TestRefundPublicAPI:
    """保证 3 文件导出的符号名 + 调用方式不变（外部模块依赖）。"""

    def test_refund_graph_exports_expected_constants(self):
        """refund_graph 公共常量可访问，类型符合预期。"""
        from app.services.refund_graph import REFUND_WINDOW_DAYS, DELIVERY_OFFSET_DAYS

        assert isinstance(REFUND_WINDOW_DAYS, int)
        assert isinstance(DELIVERY_OFFSET_DAYS, int)

    def test_refund_tool_class_attribute_accessible(self):
        """RefundTool.REFUND_WINDOW_DAYS 类属性可访问（不变成实例属性或方法）。"""
        from app.tools.refund_tool import RefundTool

        # 类属性访问（不实例化）
        assert hasattr(RefundTool, "REFUND_WINDOW_DAYS")
        assert isinstance(RefundTool.REFUND_WINDOW_DAYS, int)
        # 实例访问也要 work（Python 类属性继承）
        assert isinstance(RefundTool().REFUND_WINDOW_DAYS, int)

    def test_order_lifecycle_module_level_constant(self):
        """order_lifecycle.DELIVERY_OFFSET_DAYS 模块级可访问。"""
        from app.services import order_lifecycle

        assert hasattr(order_lifecycle, "DELIVERY_OFFSET_DAYS")
        assert isinstance(order_lifecycle.DELIVERY_OFFSET_DAYS, int)


# =============================================================
# 3. fail-fast 行为
# =============================================================
class TestRefundFailFast:
    """refund.yaml 缺失 → 任一文件 import 阶段即 ConfigError（fail-fast）。"""

    def test_missing_refund_yaml_raises_at_refund_graph_import(self, monkeypatch, tmp_path):
        """BUSINESS_RULES_DIR 指向无 refund.yaml 的目录 → import refund_graph 触发 ConfigError。

        M14 V3 变更：refund_graph 现在还在 import 期加载 decide.yaml（用于 decide_node 配置）。
        隔离目录需要同时含 decide.yaml（让 decide 通过）+ 缺 refund.yaml（让 refund 失败）。

        关键：importlib.reload 失败会留下**半初始化**的模块（_RULES/CONFIDENCE_THRESHOLD 等
        已绑定到 stub decide.yaml 的值），污染下游测试。必须在 finally 里恢复原模块状态。
        """
        from app.core import config
        from app.services.config_loader import ConfigError, reset_config_loader

        # 隔离：创建有 guard.yaml + decide.yaml 但缺 refund.yaml 的目录
        empty_dir = tmp_path / "empty_rules"
        empty_dir.mkdir()
        (empty_dir / "guard.yaml").write_text("MIN_LEN: 2\n", encoding="utf-8")
        # 完整 stub decide.yaml（避免 KeyError: 'MAX_LLM_RETRIES' 让 reload 在更早的行就挂掉）
        (empty_dir / "decide.yaml").write_text(
            "CONFIDENCE_THRESHOLD: 0.5\n"
            "MAX_LLM_RETRIES: 3\n"
            "STATUS_ZH_MAP: {}\n"
            "HARD_RULES: []\n"
            "IMAGE_URLS_OVERRIDE: {}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config.settings, "BUSINESS_RULES_DIR", str(empty_dir))
        reset_config_loader()

        # 备份原模块 globals（reload 失败会污染它们）
        import app.services.refund_graph as rg
        saved_globals = {
            k: v for k, v in rg.__dict__.items()
            if not k.startswith("__") and k.isupper()
        }

        try:
            # import refund_graph 应失败（找不到 refund.yaml）
            with pytest.raises(ConfigError, match="业务规则不存在: refund"):
                import importlib
                importlib.reload(rg)
        finally:
            # 恢复原模块 globals + 清理 singleton（让下游测试重新从真实 dir 加载）
            for k, v in saved_globals.items():
                setattr(rg, k, v)
            reset_config_loader()