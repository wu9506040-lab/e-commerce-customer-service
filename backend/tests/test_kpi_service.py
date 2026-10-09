"""WP1 业务指标 KPI 测试（kpi_service + ratings 入口）

- KPI-1 upsert_rating 新建/覆盖/非法值
- KPI-2 compute_kpi：deflection/csat/拦截率/平均轮次/P0 占比 计算正确
- KPI-3 空窗口不炸、分母为 0 返回 None（不得用 0 冒充）
- KPI-4 compute_trend 按日分桶
- KPI-5 ratings API handler 直调（含匿名 user_id=0）
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app.api.ratings import RatePayload, rate as rate_handler
from app.models.handoff_ticket import HandoffTicket
from app.models.message import Message
from app.models.message_rating import MessageRating
from app.models.operation_log import OperationLog
from app.services import kpi_service


def _msg(db, sid, uid, role, mins_ago=0):
    m = Message(session_id=sid, user_id=uid, role=role, content=f"{role} msg")
    m.create_time = dt.datetime.now() - dt.timedelta(minutes=mins_ago)
    db.add(m)
    db.commit()


def _ticket(db, sid, priority="P2"):
    t = HandoffTicket(
        ticket_no=f"HFT-{sid}", session_id=sid, user_id=1,
        reason="user_requested", priority=priority, status="pending",
    )
    t.create_time = dt.datetime.now()
    db.add(t)
    db.commit()


class TestUpsertRating:

    def test_kpi1_create_then_overwrite(self, db_session):
        r1 = kpi_service.upsert_rating(db_session, session_id="s1", user_id=7, message_id=1, rating="up")
        assert r1["ok"]
        r2 = kpi_service.upsert_rating(db_session, session_id="s1", user_id=7, message_id=1, rating="down")
        assert r2["ok"]
        rows = db_session.execute(select(MessageRating)).scalars().all()
        assert len(rows) == 1 and rows[0].rating == "down"  # 覆盖而非新增
        bad = kpi_service.upsert_rating(db_session, session_id="s1", user_id=7, message_id=1, rating="meh")
        assert not bad["ok"]


class TestComputeKpi:

    def test_kpi2_full_window(self, db_session):
        # 3 个会话；s2 转人工（P0）；1 条拦截；评价 s1=up, s3=down
        for sid, uid in [("s1", 1), ("s2", 2), ("s3", 3)]:
            _msg(db_session, sid, uid, "user")
            _msg(db_session, sid, uid, "assistant")
        _ticket(db_session, "s2", priority="P0")
        log = OperationLog(user_id=9, action="chat_guard_blocked", detail={"layer": "L1"})
        log.create_time = dt.datetime.now()
        db_session.add(log)
        db_session.commit()
        kpi_service.upsert_rating(db_session, session_id="s1", user_id=1, message_id=0, rating="up")
        kpi_service.upsert_rating(db_session, session_id="s3", user_id=3, message_id=0, rating="down")

        k = kpi_service.compute_kpi(
            db_session,
            dt.datetime.now() - dt.timedelta(hours=1),
            dt.datetime.now() + dt.timedelta(hours=1),
        )
        assert k["sessions_total"] == 3
        assert k["deflection_rate"] == round(2 / 3, 4)
        assert k["handoff_rate"] == round(1 / 3, 4)
        assert k["csat"] == 0.5
        assert k["rating_coverage"] == round(2 / 3, 4)
        assert k["guard_blocked"] == 1
        assert k["avg_rounds"] == 1.0
        assert k["p0_ratio"] == 1.0

    def test_kpi3_empty_window(self, db_session):
        k = kpi_service.compute_kpi(
            db_session,
            dt.datetime(2020, 1, 1), dt.datetime(2020, 1, 2),
        )
        assert k["sessions_total"] == 0
        assert k["deflection_rate"] is None
        assert k["csat"] is None


class TestTrendAndApi:

    def test_kpi4_trend_buckets(self, db_session):
        _msg(db_session, "s9", 1, "user")
        _ticket(db_session, "s9")
        t = kpi_service.compute_trend(db_session, days=7)
        assert len(t["series"]) == 7
        today = t["series"][-1]
        assert today["sessions"] >= 1
        assert today["handoffs"] >= 1

    def test_kpi5_api_handler_anonymous(self, db_session):
        resp = rate_handler(
            RatePayload(session_id="s5", rating="up", comment="有用"),
            user=None, db=db_session,
        )
        assert resp["ok"]
        row = db_session.execute(select(MessageRating)).scalars().one()
        assert row.user_id == 0 and row.rating == "up" and row.comment == "有用"
