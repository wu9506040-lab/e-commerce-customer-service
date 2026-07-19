"""tests/services/test_escalation_categories.py

M14 V3 escalation_service 扩展单测（真实工作流重构配套）

覆盖（12 case）：
  1. P0 关键词检测（4 类：complaint / compensation / quality / user_requested）= 4 case
  2. P0 优先级排序（COMPLAINT/COMPENSATION > QUALITY/USER_REQUESTED）= 1 case
  3. HandoffPayload +4 字段（priority / category / matched_keyword / detected_category）= 4 case
  4. HandoffService.handoff() 接受 priority/category 参数 = 2 case
  5. detect_handoff_keyword 兼容性（保留向后兼容）= 1 case
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://u:p@localhost:3306/db?charset=utf8mb4")

from app.services.escalation_service import (  # noqa: E402
    EscalationReason,
    HandoffPayload,
    detect_handoff_keyword,
    detect_p0_escalate,
    get_escalation_service,
    reset_escalation_service,
    ESCALATE_P0_KEYWORDS,
)


# =============================================================
# 1. P0 关键词检测（4 类）
# =============================================================
class TestP0KeywordDetection:
    """P1-1：detect_p0_escalate 4 类关键词命中"""

    def test_complaint_keywords(self):
        """投诉类关键词命中（complaint category）"""
        for kw in ["投诉", "12315", "12305", "曝光", "315", "工商局", "市监"]:
            result = detect_p0_escalate(f"我{kw}你们")
            assert result is not None, f"应识别 P0 投诉关键词: {kw}"
            category, matched = result
            assert category == "complaint", f"{kw} 应归类 complaint，实际 {category}"
            assert matched == kw

    def test_compensation_keywords(self):
        """赔付类关键词命中（compensation category）"""
        for kw in ["三倍赔偿", "退一赔三", "假一赔十"]:
            result = detect_p0_escalate(f"我要{kw}")
            assert result is not None, f"应识别 P0 赔付关键词: {kw}"
            category, matched = result
            assert category == "compensation"
            assert matched == kw

    def test_quality_keywords(self):
        """质量类关键词命中（quality category，扩展自原 _HANDOFF_KEYWORDS）"""
        # 真实话术命中词（RC005/015/056/069/077/095/097）
        for kw in ["质量问题", "破损", "坏点", "开胶", "假货", "二手商品"]:
            result = detect_p0_escalate(f"商品{kw}")
            assert result is not None, f"应识别 P0 质量关键词: {kw}"
            category, matched = result
            assert category == "quality"
            assert matched == kw

    def test_user_requested_keywords(self):
        """主动要人工关键词命中（user_requested category）"""
        for kw in ["转人工", "转主管", "机器人", "起诉", "律师"]:
            result = detect_p0_escalate(f"我要{kw}")
            assert result is not None, f"应识别 P0 用户主动关键词: {kw}"
            category, matched = result
            assert category == "user_requested"
            assert matched == kw


# =============================================================
# 2. P0 优先级排序（COMPLAINT/COMPENSATION > QUALITY/USER_REQUESTED）
# =============================================================
class TestP0PriorityOrder:
    """多关键词命中时优先级排序"""

    def test_complaint_over_quality(self):
        """同时含"投诉"和"质量" → complaint 优先（P0 > P1）"""
        result = detect_p0_escalate("商品质量有问题，我要投诉 12315")
        assert result is not None
        category, matched = result
        assert category == "complaint", \
            "投诉类应优先于质量类（P0 > P1）"
        assert matched == "12315" or matched == "投诉"

    def test_compensation_over_user_requested(self):
        """同时含"三倍赔偿"和"转人工" → compensation 优先"""
        result = detect_p0_escalate("质量这么差要三倍赔偿，给我转人工！")
        assert result is not None
        category, matched = result
        assert category == "compensation", "赔付类应优先于主动要人工"


# =============================================================
# 3. HandoffPayload +4 字段（向后兼容）
# =============================================================
class TestHandoffPayloadFields:
    """HandoffPayload 扩展字段：priority/category/matched_keyword/detected_category"""

    def test_payload_with_priority(self):
        """priority 字段可读写"""
        payload = HandoffPayload(
            handoff_id="H12345678",
            reason="user_requested",
            reason_label="已为您转接人工客服",
            created_at="2026-07-19T00:00:00Z",
            user_id=1,
            priority="P0",
            category="投诉",
            matched_keyword="12315",
            detected_category="complaint",
        )
        d = payload.to_dict()
        assert d["priority"] == "P0"
        assert d["category"] == "投诉"
        assert d["matched_keyword"] == "12315"
        assert d["detected_category"] == "complaint"

    def test_payload_default_none(self):
        """未指定 priority/category 时默认为 None（向后兼容）"""
        payload = HandoffPayload(
            handoff_id="H12345678",
            reason="agent_unavailable",
            reason_label="系统繁忙",
            created_at="2026-07-19T00:00:00Z",
            user_id=1,
        )
        d = payload.to_dict()
        assert d["priority"] is None
        assert d["category"] is None
        assert d["matched_keyword"] is None
        assert d["detected_category"] is None

    def test_payload_to_dict_includes_all_fields(self):
        """to_dict() 必须包含新增 4 字段（SSE 协议扩展）"""
        payload = HandoffPayload(
            handoff_id="H12345678",
            reason="user_requested",
            reason_label="",
            created_at="",
            user_id=1,
            priority="P1",
        )
        d = payload.to_dict()
        for field in ("priority", "category", "matched_keyword", "detected_category"):
            assert field in d, f"HandoffPayload.to_dict() 必须包含 {field} 字段"

    def test_payload_optional_fields_no_required(self):
        """新增字段全是 Optional，不破坏现有调用"""
        # 模拟现有调用：仅传必填字段
        try:
            payload = HandoffPayload(
                handoff_id="H1",
                reason="agent_unavailable",
                reason_label="",
                created_at="",
                user_id=1,
            )
            assert payload.priority is None  # 默认 None
        except TypeError as e:
            pytest.fail(f"现有调用方式被破坏：{e}")


# =============================================================
# 4. EscalationService.handoff() 接受 priority/category
# =============================================================
class TestHandoffServiceExtended:
    """handoff() 方法接受 priority/category/matched_keyword/detected_category"""

    def setup_method(self):
        reset_escalation_service()

    def test_handoff_with_priority_p0(self):
        """handoff(reason=BUSINESS_RULE, priority="P0", category="投诉")"""
        svc = get_escalation_service()
        with patch("app.services.escalation_service.OrderTool") as mock_tool:
            mock_tool.list_user_orders.return_value = [{"order_no": "O001"}]
            payload = svc.handoff(
                reason=EscalationReason.BUSINESS_RULE,
                user_id=42,
                priority="P0",
                category="投诉",
                matched_keyword="12315",
                detected_category="complaint",
            )
            assert payload.priority == "P0"
            assert payload.category == "投诉"
            assert payload.matched_keyword == "12315"
            assert payload.detected_category == "complaint"

    def test_handoff_without_priority_defaults_none(self):
        """handoff() 不传 priority → 默认 None（向后兼容）"""
        svc = get_escalation_service()
        with patch("app.services.escalation_service.OrderTool") as mock_tool:
            mock_tool.list_user_orders.return_value = []
            payload = svc.handoff(
                reason=EscalationReason.AGENT_UNAVAILABLE,
                user_id=1,
            )
            assert payload.priority is None
            assert payload.category is None


# =============================================================
# 5. detect_handoff_keyword 向后兼容
# =============================================================
class TestDetectHandoffKeywordCompat:
    """原 detect_handoff_keyword（9 词）仍工作"""

    def test_existing_keywords_still_work(self):
        """原 _HANDOFF_KEYWORDS 9 词继续可用"""
        for kw in ["转人工", "人工客服", "真人客服", "找人工", "转接人工"]:
            assert detect_handoff_keyword(kw), f"原关键词应继续工作: {kw}"

    def test_p0_keywords_also_work_via_old_detector(self):
        """P0 关键词也通过 detect_handoff_keyword（向后兼容）"""
        # detect_p0_escalate 是新的，detect_handoff_keyword 是旧的
        # P0 关键词命中 detect_p0_escalate 后由 chat.py 上层拦截
        # 不应让 detect_handoff_keyword 拦截（避免重复拦截）
        # 实际行为：detect_handoff_keyword 只匹配原 9 词"转人工"类
        for kw in ["12315", "三倍赔偿", "投诉"]:  # 这些不在 _HANDOFF_KEYWORDS
            assert not detect_handoff_keyword(kw), \
                f"{kw} 应交给 detect_p0_escalate，不在 detect_handoff_keyword"


# =============================================================
# 6. ESCALATE_P0_KEYWORDS 配置可访问
# =============================================================
class TestEscalateP0KeywordsDict:
    """ESCALATE_P0_KEYWORDS 是 dict，可外部访问（前端/audit 用）"""

    def test_dict_has_4_categories(self):
        """dict 必须含 4 个 category"""
        assert "complaint" in ESCALATE_P0_KEYWORDS
        assert "compensation" in ESCALATE_P0_KEYWORDS
        assert "quality" in ESCALATE_P0_KEYWORDS
        assert "user_requested" in ESCALATE_P0_KEYWORDS

    def test_dict_values_are_tuples_of_strings(self):
        """value 是 string tuple"""
        for cat, kws in ESCALATE_P0_KEYWORDS.items():
            assert isinstance(kws, tuple), f"{cat} 应为 tuple"
            assert all(isinstance(kw, str) for kw in kws), f"{cat} 元素应为 str"