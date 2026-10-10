"""M15 人工介入工单闭环测试（服务层）

覆盖：
- HT-1 persist_handoff 建单 → list_tickets 可见
- HT-2 同 session 重复合并（不刷屏建多张）
- HT-3 take 认领状态流转 pending→taken；重复认领 409
- HT-4 resolve 人工回复 → 注入会话 messages（role=assistant + human_agent 标记）+ 状态 resolved
- HT-5 get_ticket_detail 带最近消息 + is_human_agent 标记正确
- HT-6 persist 失败不抛（best-effort，主链路无感）
"""
import pytest

from app.models.message import Message
from app.services import handoff_ticket_service as svc


def _payload(sid="S-abc"):
    return {
        "handoff_id": "H12345ABC",
        "reason": "user_requested",
        "user_id": 42,
        "summary_text": "用户要求转人工",
        "recent_messages": [{"role": "user", "content": "我要找人工"}],
    }


class TestTicketLifecycle:

    def test_ht1_persist_then_list(self, db_session):
        tid = svc.persist_handoff(
            user_id=42, session_id="S-abc", reason="user_requested",
            payload_dict=_payload(), priority="P1", db=db_session,
        )
        assert tid is not None
        out = svc.list_tickets(db_session, status="pending")
        assert out["total"] == 1
        assert out["items"][0]["ticket_no"].startswith("HFT-")
        assert out["items"][0]["priority"] == "P1"

    def test_ht2_merge_same_session(self, db_session):
        t1 = svc.persist_handoff(42, "S-dup", "user_requested", _payload(), db=db_session)
        t2 = svc.persist_handoff(42, "S-dup", "user_requested", _payload(), priority="P0", db=db_session)
        assert t1 == t2, "同 session 未结单应合并而非新建"
        assert svc.list_tickets(db_session, status="pending")["total"] == 1
        # 升级词把 P2 抬到 P0
        assert svc.list_tickets(db_session)["items"][0]["priority"] == "P0"

    def test_ht3_take_flow_and_conflict(self, db_session):
        tid = svc.persist_handoff(42, "S-take", "user_requested", _payload(), db=db_session)
        ok = svc.take_ticket(db_session, tid, "agent_wang")
        assert ok["ok"] and ok["ticket"]["status"] == "taken"
        assert ok["ticket"]["assignee"] == "agent_wang"
        conflict = svc.take_ticket(db_session, tid, "agent_li")
        assert not conflict["ok"] and conflict["code"] == 409

    def test_ht4_resolve_injects_message(self, db_session):
        tid = svc.persist_handoff(42, "S-res", "user_requested", _payload(), db=db_session)
        svc.take_ticket(db_session, tid, "agent_wang")
        res = svc.resolve_ticket(db_session, tid, "agent_wang", "您好，已为您优先处理退款。")
        assert res["ok"] and res["ticket"]["status"] == "resolved"
        # 人工回复作为 assistant 注入该会话
        msg = db_session.query(Message).filter_by(session_id="S-res", role="assistant").one()
        assert "优先处理退款" in msg.content
        assert msg.contexts and msg.contexts.get("human_agent_ticket")

    def test_ht5_detail_with_messages(self, db_session):
        tid = svc.persist_handoff(42, "S-det", "user_requested", _payload(), db=db_session)
        svc.resolve_ticket(db_session, tid, "agent", "已处理")
        detail = svc.get_ticket_detail(db_session, tid)
        assert detail["reply"] == "已处理"
        assert any(m["is_human_agent"] for m in detail["messages"])

    def test_ht6_persist_best_effort_no_throw(self, db_session):
        # 传入无 execute 的坏对象模拟 DB 故障：不应抛出，返回 None
        class _Broken:
            def execute(self, *a, **k): raise RuntimeError("db down")
        tid = svc.persist_handoff(42, "S-fail", "user_requested", _payload(), db=_Broken())
        assert tid is None

    def test_ht7_resolve_invalidates_redis_history(self, db_session):
        """V13(1.4)：resolve 必须失效该会话 Redis 热历史——用户拉历史 Redis
        优先，不清热缓存则永远看不到坐席回复（M15 宣称的真实断链）"""
        from unittest.mock import patch

        tid = svc.persist_handoff(
            42, "S-inv", "user_requested", _payload(sid="S-inv"), db=db_session
        )
        with patch("app.services.redis_store.clear_history") as mock_clear:
            res = svc.resolve_ticket(db_session, tid, "agent", "已处理，请查收。")
        assert res["ok"]
        mock_clear.assert_called_once_with("S-inv")

    def test_ht7b_clear_failure_does_not_break_resolve(self, db_session):
        """V13(1.4) 边界：Redis 不可用时 resolve 照常成功（TTL 兜底，best-effort）"""
        from unittest.mock import patch

        tid = svc.persist_handoff(
            42, "S-inv2", "user_requested", _payload(sid="S-inv2"), db=db_session
        )
        with patch("app.services.redis_store.clear_history",
                   side_effect=ConnectionError("redis down")):
            res = svc.resolve_ticket(db_session, tid, "agent", "已处理")
        assert res["ok"] and res["ticket"]["status"] == "resolved"
