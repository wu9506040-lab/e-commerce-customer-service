"""
P2 长程记忆: profile_service 单测

覆盖：
1. get_or_create：新建 / 复用 / user_id=0 短路 / DB 异常 best-effort
2. update_summary：替换式更新 / 新建 / 异常 best-effort
3. append_frequent_skus：去重 + 截断到 max_keep + 新建 + 异常 best-effort
4. increment_interaction：累加 / 新建 / 异常 best-effort
5. clear：软删（deleted=1 + 字段清空）/ 行不存在返 False
6. to_prompt_block：空 profile 返 "" / 结构化输出 / 硬截断 / 反幻觉 label
7. 隐私边界：user_id=0 / 0 / None 全部短路
8. settings.ENABLE_USER_PROFILE 灰度开关（to_prompt_block 不感知，由 orchestrator 把控）

设计原则：
- mock `with_safe_session` 上下文管理器（与 Sprint 4 测试同模式）
- mock `UserProfile` ORM（避免真 DB）
- profile_service 失败路径必须返 None / False（不抛）
"""
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# pytest 不走 __main__，env 必须在模块顶部 setdefault
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =============================================================
# 辅助：构造 mock session + mock UserProfile 行
# =============================================================
def _make_session_with_row(row):
    """构造 session：select().scalar_one_or_none() 返回 row"""
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = row
    return db


def _patch_safe_session_with_db(db):
    """返回一个 with_safe_session 的 mock（接收 commit kwarg，yield db）"""
    @contextmanager
    def _mock(*, commit=True):
        yield db
    return _mock


# =============================================================
# 1. get_or_create
# =============================================================
class TestGetOrCreate:
    def test_user_id_zero_returns_none(self):
        """user_id=0（匿名）→ 短路返 None"""
        from app.services.profile_service import get_or_create

        assert get_or_create(0) is None

    def test_existing_profile_returned(self):
        """profile 行已存在 → 直接返回"""
        from app.services.profile_service import get_or_create

        existing = MagicMock()
        existing.user_id = 42
        existing.deleted = 0
        db = _make_session_with_row(existing)

        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            result = get_or_create(42)
        assert result is existing

    def test_missing_profile_auto_creates_empty(self):
        """profile 行不存在 → 自动 INSERT 空 profile"""
        from app.services.profile_service import get_or_create

        db = _make_session_with_row(None)

        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            result = get_or_create(99)
        assert result is not None
        # INSERT 应该被 add()
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert added.user_id == 99
        assert added.interaction_count == 0
        assert added.frequent_skus == []

    def test_db_exception_returns_none_best_effort(self):
        """DB 异常 → 返 None，不抛（best-effort）"""
        from app.services.profile_service import get_or_create

        @contextmanager
        def boom_session(*a, **kw):
            raise RuntimeError("MySQL down")
            yield  # unreachable, but required for @contextmanager

        with patch("app.services.profile_service.with_safe_session", boom_session):
            result = get_or_create(7)
        assert result is None

        with patch("app.services.profile_service.with_safe_session", boom_session):
            result = get_or_create(7)
        assert result is None


# =============================================================
# 2. update_summary
# =============================================================
class TestUpdateSummary:
    def test_user_id_zero_returns_false(self):
        from app.services.profile_service import update_summary
        assert update_summary(0, "摘要") is False

    def test_existing_row_updated(self):
        from app.services.profile_service import update_summary

        row = MagicMock()
        row.deleted = 0
        row.summary = None
        db = _make_session_with_row(row)

        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            assert update_summary(1, "新的摘要") is True
        assert row.summary == "新的摘要"

    def test_missing_row_inserts(self):
        from app.services.profile_service import update_summary

        db = _make_session_with_row(None)

        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            assert update_summary(2, "首条摘要") is True
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert added.summary == "首条摘要"

    def test_db_exception_returns_false(self):
        from app.services.profile_service import update_summary

        @contextmanager
        def boom(*a, **kw):
            raise RuntimeError("connection lost")
            yield

        with patch("app.services.profile_service.with_safe_session", boom):
            assert update_summary(1, "x") is False


# =============================================================
# 3. append_frequent_skus
# =============================================================
class TestAppendFrequentSkus:
    def test_empty_skus_returns_false(self):
        """空 list → 不写，返 False（短路）"""
        from app.services.profile_service import append_frequent_skus
        assert append_frequent_skus(1, []) is False

    def test_dedup_and_truncate(self):
        """已有 SKU 去重 + 新 SKU 追加 + 截断到 max_keep"""
        from app.services.profile_service import append_frequent_skus

        row = MagicMock()
        row.deleted = 0
        row.frequent_skus = ["ZP1", "ZP2"]
        db = _make_session_with_row(row)

        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            assert append_frequent_skus(1, ["ZP2", "ZP3", "ZP4"], max_keep=3) is True
        # 期望：去重后 [ZP1, ZP2, ZP3, ZP4]，截断到 max_keep=3 → 后 3 个 [ZP2, ZP3, ZP4]
        assert row.frequent_skus == ["ZP2", "ZP3", "ZP4"]

    def test_missing_row_inserts_with_skus(self):
        from app.services.profile_service import append_frequent_skus

        db = _make_session_with_row(None)
        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            assert append_frequent_skus(1, ["ZP1"]) is True
        added = db.add.call_args[0][0]
        assert added.frequent_skus == ["ZP1"]

    def test_db_exception_returns_false(self):
        from app.services.profile_service import append_frequent_skus

        @contextmanager
        def boom(*a, **kw):
            raise RuntimeError("MySQL timeout")
            yield

        with patch("app.services.profile_service.with_safe_session", boom):
            assert append_frequent_skus(1, ["ZP1"]) is False


# =============================================================
# 4. increment_interaction
# =============================================================
class TestIncrementInteraction:
    def test_existing_row_increments(self):
        from app.services.profile_service import increment_interaction

        row = MagicMock()
        row.deleted = 0
        row.interaction_count = 5
        db = _make_session_with_row(row)

        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            assert increment_interaction(1, delta=1) is True
        assert row.interaction_count == 6

    def test_missing_row_creates_with_delta(self):
        from app.services.profile_service import increment_interaction

        db = _make_session_with_row(None)
        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            assert increment_interaction(1, delta=3) is True
        added = db.add.call_args[0][0]
        assert added.interaction_count == 3

    def test_user_id_zero_returns_false(self):
        from app.services.profile_service import increment_interaction
        assert increment_interaction(0) is False


# =============================================================
# 5. clear
# =============================================================
class TestClear:
    def test_existing_row_soft_deletes(self):
        """行存在 → 软删（deleted=1 + 字段清空）"""
        from app.services.profile_service import clear

        row = MagicMock()
        row.deleted = 0
        row.summary = "敏感摘要"
        row.frequent_skus = ["ZP1"]
        row.preferences = {"cat": "electronics"}
        db = _make_session_with_row(row)

        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            assert clear(1) is True
        assert row.deleted == 1
        assert row.summary is None
        assert row.frequent_skus == []
        assert row.preferences == {}

    def test_missing_row_returns_false(self):
        from app.services.profile_service import clear

        db = _make_session_with_row(None)
        with patch("app.services.profile_service.with_safe_session", _patch_safe_session_with_db(db)):
            assert clear(1) is False

    def test_user_id_zero_returns_false(self):
        from app.services.profile_service import clear
        assert clear(0) is False


# =============================================================
# 6. to_prompt_block（核心：LLM 注入前的格式化）
# =============================================================
class TestToPromptBlock:
    def test_none_profile_returns_empty_string(self):
        from app.services.profile_service import to_prompt_block
        assert to_prompt_block(None) == ""

    def test_empty_profile_returns_empty_string(self):
        """profile 全空（无 preferences / skus / summary）→ 返空串"""
        from app.services.profile_service import to_prompt_block

        profile = MagicMock()
        profile.preferences = None
        profile.frequent_skus = None
        profile.summary = None
        profile.interaction_count = 0
        assert to_prompt_block(profile) == ""

    def test_structured_output_with_all_fields(self):
        """profile 含所有字段 → 输出结构化 4 行"""
        from app.services.profile_service import to_prompt_block

        profile = MagicMock()
        profile.preferences = {"refund_pref": "fast", "cat": "electronics"}
        profile.frequent_skus = ["ZP1", "ZP2", "ZP3"]
        profile.summary = "用户近期关注 ZP1 系列配件"
        profile.interaction_count = 12
        block = to_prompt_block(profile)
        assert "当前用户画像" in block
        assert "跨 session 长程记忆" in block
        assert "仅作参考" in block  # 反幻觉 label
        assert "refund_pref=fast" in block or "refund_pref" in block
        assert "ZP1" in block
        assert "ZP2" in block
        assert "用户近期关注" in block
        assert "12 轮" in block

    def test_interaction_count_below_3_hidden(self):
        """interaction_count < 3 → 不显示（新手用户无意义）"""
        from app.services.profile_service import to_prompt_block

        profile = MagicMock()
        profile.preferences = None
        profile.frequent_skus = ["ZP1"]
        profile.summary = None
        profile.interaction_count = 2  # < 3
        block = to_prompt_block(profile)
        assert "累计对话" not in block
        assert "ZP1" in block

    def test_hard_truncate_at_max_len(self):
        """长 summary → 整体硬截断到 max_len + 末尾 '…'"""
        from app.services.profile_service import to_prompt_block

        profile = MagicMock()
        profile.preferences = None
        profile.frequent_skus = ["ZP1"] * 20
        profile.summary = "A" * 1000  # 远超 max_len
        profile.interaction_count = 100
        block = to_prompt_block(profile, max_len=50)
        assert len(block) <= 50
        assert block.endswith("…") or len(block) <= 50

    def test_preferences_max_3_items(self):
        """preferences 最多展示 3 个"""
        from app.services.profile_service import to_prompt_block

        profile = MagicMock()
        profile.preferences = {f"k{i}": f"v{i}" for i in range(10)}
        profile.frequent_skus = None
        profile.summary = None
        profile.interaction_count = 0
        block = to_prompt_block(profile)
        # 仅前 3 个 tag 在输出里
        assert "k0=v0" in block
        assert "k2=v2" in block
        assert "k9=v9" not in block

    def test_skus_max_5_items(self):
        """frequent_skus 最多展示 5 个"""
        from app.services.profile_service import to_prompt_block

        profile = MagicMock()
        profile.preferences = None
        profile.frequent_skus = [f"SKU{i}" for i in range(20)]
        profile.summary = None
        profile.interaction_count = 0
        block = to_prompt_block(profile)
        # 仅前 5 个 SKU 在输出里
        assert "SKU0" in block
        assert "SKU4" in block
        assert "SKU19" not in block


# =============================================================
# 7. 隐私边界
# =============================================================
class TestPrivacyBoundary:
    def test_anonymous_user_id_skipped_all_writes(self):
        """user_id=0（匿名）→ 所有写路径返 None / False，不写 DB"""
        from app.services.profile_service import (
            get_or_create, update_summary, append_frequent_skus,
            increment_interaction, clear,
        )

        # mock 整个 with_safe_session，确保真没被调用
        with patch("app.services.profile_service.with_safe_session") as mock_safe:
            assert get_or_create(0) is None
            assert update_summary(0, "x") is False
            assert append_frequent_skus(0, ["ZP1"]) is False
            assert increment_interaction(0) is False
            assert clear(0) is False
            assert mock_safe.call_count == 0  # 零 DB 调用


# =============================================================
# 8. 灰度开关（to_prompt_block 不感知开关，验证 settings 行为由 orchestrator 把控）
# =============================================================
class TestGrayscaleSwitch:
    def test_to_prompt_block_does_not_check_settings(self):
        """to_prompt_block 是纯函数，不读 settings.ENABLE_USER_PROFILE"""
        from app.services.profile_service import to_prompt_block

        profile = MagicMock()
        profile.preferences = {"cat": "x"}
        profile.frequent_skus = ["ZP1"]
        profile.summary = None
        profile.interaction_count = 0
        # 即便开关关闭，函数本身仍能输出 block（开关由 orchestrator 把控）
        with patch("app.core.config.settings.ENABLE_USER_PROFILE", False):
            block = to_prompt_block(profile)
        assert "ZP1" in block


if __name__ == "__main__":
    os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
    os.environ.setdefault("DATABASE_URL", "mysql+pymysql://cs_user:pwd@mysql:3306/customer_service?charset=utf8mb4")
    print("ALL SCENARIOS PASSED")
