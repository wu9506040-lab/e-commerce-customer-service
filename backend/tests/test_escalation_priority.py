"""V13（1.5，2026-10-10）：转人工词表分级 + guard.yaml 覆盖一致性

审计背景：原"user_requested"类把 转人工/机器人 与 起诉/律师 混在一起全判 P0，
"转人工"是路由诉求不是风险事件，霸占 P0 队列稀释真正的高风险工单
（随口一句"你是不是机器人"也升 P0）。拆分：
- user_requested（起诉/律师）= P0 法律行动
- user_transfer（转人工类）= P1 正常升级排队
词表与优先级现可被 guard.yaml ESCALATE_P0_* 覆盖，代码内置为兜底，
本文件同时锁定两处一致性（防双源漂移）。
"""
import os

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x")

from app.services.escalation_service import (
    ESCALATE_P0_KEYWORDS,
    detect_p0_escalate,
    get_p0_category_info,
)


class TestPriorityGrading:

    def test_plain_transfer_is_p1(self):
        hit = detect_p0_escalate("转人工")
        assert hit is not None and hit[0] == "user_transfer"
        assert get_p0_category_info("user_transfer")[0] == "P1"

    def test_bot_word_no_longer_p0(self):
        """'你是不是机器人'类口语不再触发 P0（原实现中招）"""
        hit = detect_p0_escalate("你是不是机器人啊")
        assert hit is not None and hit[0] == "user_transfer"
        assert get_p0_category_info("user_transfer")[0] == "P1"

    def test_legal_threat_stays_p0(self):
        hit = detect_p0_escalate("再不处理我就起诉、找律师")
        assert hit is not None and hit[0] == "user_requested"
        assert get_p0_category_info("user_requested")[0] == "P0"

    def test_complaint_priority_over_transfer(self):
        """同时含'投诉'与'转人工'→ complaint 优先（高风险 > 路由诉求）"""
        hit = detect_p0_escalate("我要投诉你们，转人工！")
        assert hit is not None and hit[0] == "complaint"
        assert get_p0_category_info("complaint")[0] == "P0"

    def test_guard_yaml_override_consistency(self):
        """guard.yaml 是覆盖源：类别集合包含内置默认、P1 分级两处一致"""
        from app.services.config_loader import get_config_loader
        g = get_config_loader().load("guard") or {}
        yk = g.get("ESCALATE_P0_KEYWORDS") or {}
        assert yk, "guard.yaml 应携带 ESCALATE_P0_KEYWORDS 段"
        assert set(yk) <= set(ESCALATE_P0_KEYWORDS)  # 加载后服务字典是并集
        assert yk.get("user_requested") == ["起诉", "律师"]
        assert g["ESCALATE_P0_PRIORITY"]["user_transfer"] == "P1"
