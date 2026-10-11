"""V14（human-in-the-loop）confirm_ticket 测试

覆盖：
1. propose：写入 + 同 args_hash 去重 bump（同 session/tool）
2. 5 道防御：
   ① 参数冻结 args_hash
   ② TTL 过期
   ③ 幂等（已 executed 二次 confirm 返原 result，不重复执行）
   ④ 状态机（仅 proposed→confirmed/rejected，merged 锁定）
   ⑤ 全量 timestamp + result_snapshot
3. 跨用户权限：别人的 ticket 不能 confirm/reject
4. sweep_expired：过期自动状态推进
5. 风险分级（settings 配置 + executor 注册）
"""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime as dt
from typing import List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.confirm_ticket import (
    ConfirmTicket,
    STATUS_EXECUTED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
)
from app.models.user import User
from app.services import confirm_ticket_service as ct
from app.services.metrics import metrics


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def alice(db):
    u = User(id=1, username="alice", password_hash="x", role="user", status=1)
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def bob(db):
    u = User(id=2, username="bob", password_hash="x", role="user", status=1)
    db.add(u)
    db.commit()
    return u


def _register_executor(echo: List[dict]):
    """注册一个 echo 执行器：把 args 拷进列表，便于断言调用次数（防双花）"""
    def _exec(args, user_id):
        echo.append({"args": dict(args), "user_id": user_id})
        return {"status": "ok", "echo": args.get("order_no")}
    ct.register_executor("refund_execute", _exec)
    return _exec


# =============================================================
# 1. propose 写入 + 同 args_hash 去重
# =============================================================
class TestPropose:
    def test_create_then_get(self, db, alice):
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok"})
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "ORD20260628001",
                             "reason": "不喜欢了"},
                       summary="退款 ORD20260628001 ¥99.00",
                       user_id=alice.id, session_id="s1")
        assert t["id"] > 0
        assert t["tool_name"] == "refund_execute"
        assert t["status"] == STATUS_PROPOSED
        assert t["args_hash"].startswith("ct:")
        # 再次读取详情
        again = ct.get_by_id(db, t["id"], user_id=alice.id)
        assert again["summary"] == "退款 ORD20260628001 ¥99.00"

    def test_dedupe_same_args_in_same_session(self, db, alice):
        echo: List[dict] = []
        _register_executor(echo)
        # 同会话同工具同参数二次 propose 应被去重（返回既有行，exp 顺延）
        t1 = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1", "reason": "x"},
                       summary="s", user_id=alice.id, session_id="s1", ttl_sec=300)
        t2 = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1", "reason": "x"},
                       summary="s", user_id=alice.id, session_id="s1", ttl_sec=600)
        assert t1["id"] == t2["id"]  # 同一行（去重）
        # 不同 args（参数顺序无关，hash 是 canonical）→ 不同行
        t3 = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O2"}, summary="s",
                       user_id=alice.id, session_id="s1")
        assert t3["id"] != t1["id"]

    def test_unknown_tool_rejected(self, db, alice):
        with pytest.raises(ValueError):
            ct.propose(db, tool_name="send_missile",
                       args={}, summary="x", user_id=alice.id)


# =============================================================
# 2. 5 道防御（核心）
# =============================================================
class TestFiveDefenses:
    def test_defense1_args_hash_freeze(self, db, alice):
        """① args_hash：人确认时若 AI 改了 tool_args，confirm 拒绝"""
        echo: List[dict] = []
        _register_executor(echo)
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1", "reason": "原价"},
                       summary="s", user_id=alice.id)
        # AI 在人确认前偷偷把 reason 改了——写库 vs 服务端重算 hash 必须不一致
        row = db.get(ConfirmTicket, t["id"])
        row.tool_args = {"order_no": "O1", "reason": "改价涨 5 倍"}  # 模拟 AI 篡改
        db.commit()
        with pytest.raises(ValueError) as ei:
            ct.confirm(db, t["id"], user_id=alice.id)
        assert "参数在确认时被修改" in str(ei.value)
        # 取消注册避免影响其他
        ct.TOOL_EXECUTORS.pop("refund_execute", None)

    def test_defense2_ttl_expiry(self, db, alice):
        """② TTL：超期未操作变 expired，confirm 拒绝"""
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok"})
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1"}, summary="s",
                       user_id=alice.id, ttl_sec=60)
        # 模拟时间过去
        row = db.get(ConfirmTicket, t["id"])
        row.expires_at = dt.datetime.now() - dt.timedelta(seconds=1)
        db.commit()
        with pytest.raises(ValueError) as ei:
            ct.confirm(db, t["id"], user_id=alice.id)
        # sweep 把状态推进到 expired 后再 confirm 应拒绝
        assert "过期" in str(ei.value)

    def test_defense3_idempotent_after_executed(self, db, alice):
        """③ 幂等：executed 状态二次 confirm 返原 result，不重复执行"""
        echo: List[dict] = []
        _register_executor(echo)
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1"}, summary="s", user_id=alice.id)
        ct.confirm(db, t["id"], user_id=alice.id)
        assert len(echo) == 1
        # 第二次 confirm（人误操作）→ 不应执行 executor
        again = ct.confirm(db, t["id"], user_id=alice.id)
        assert again["status"] == STATUS_EXECUTED
        assert len(echo) == 1, f"执行器被调用了 {len(echo)} 次（应为 1 次）"

    def test_defense4_status_machine_lock(self, db, alice):
        """④ 状态机：rejected 不能 confirm"""
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok"})
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1"}, summary="s", user_id=alice.id)
        ct.reject(db, t["id"], user_id=alice.id, reason="用户反悔")
        with pytest.raises(ValueError):
            ct.confirm(db, t["id"], user_id=alice.id)

    def test_defense5_full_timestamp_audit(self, db, alice):
        """⑤ 全量 timestamp + result_snapshot 可回放"""
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok", "refund_no": "RF99"})
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1"}, summary="s", user_id=alice.id)
        before = dt.datetime.now()
        ct.confirm(db, t["id"], user_id=alice.id)
        row = db.get(ConfirmTicket, t["id"])
        assert row.confirmed_at is not None and row.confirmed_at >= before
        assert row.executed_at is not None and row.executed_at >= row.confirmed_at
        assert row.result_snapshot == {"status": "ok", "refund_no": "RF99"}
        # 状态链：proposed → confirmed → executed（同一行走完三态）
        assert row.status == STATUS_EXECUTED


# =============================================================
# 3. 跨用户权限
# =============================================================
class TestCrossUserPermissions:
    def test_other_user_cannot_confirm(self, db, alice, bob):
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok"})
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1"}, summary="s", user_id=alice.id)
        with pytest.raises(PermissionError):
            ct.confirm(db, t["id"], user_id=bob.id)
        with pytest.raises(PermissionError):
            ct.reject(db, t["id"], user_id=bob.id, reason="hack")

    def test_get_other_user_ticket_403(self, db, alice, bob):
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok"})
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1"}, summary="s", user_id=alice.id)
        with pytest.raises(PermissionError):
            ct.get_by_id(db, t["id"], user_id=bob.id)


# =============================================================
# 4. sweep_expired
# =============================================================
class TestSweepExpired:
    def test_marks_old_proposed_as_expired(self, db, alice):
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok"})
        ct.propose(db, tool_name="refund_execute",
                   args={"order_no": "O1"}, summary="s", user_id=alice.id)
        # 把 expires_at 改到过去
        row = db.query(ConfirmTicket).filter(ConfirmTicket.user_id == alice.id).first()
        row.expires_at = dt.datetime.now() - dt.timedelta(seconds=1)
        db.commit()
        n = ct.sweep_expired(db)
        assert n == 1
        # 二次 sweep 应该返回 0
        assert ct.sweep_expired(db) == 0

    def test_does_not_touch_executed_or_rejected(self, db, alice):
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok"})
        t = ct.propose(db, tool_name="refund_execute",
                       args={"order_no": "O1"}, summary="s", user_id=alice.id)
        ct.confirm(db, t["id"], user_id=alice.id)
        # 已 executed 行的 expires_at 早过期——sweep 不动它
        row = db.get(ConfirmTicket, t["id"])
        row.expires_at = dt.datetime.now() - dt.timedelta(days=1)
        db.commit()
        assert ct.sweep_expired(db) == 0
        row = db.get(ConfirmTicket, t["id"])
        assert row.status == STATUS_EXECUTED


# =============================================================
# 5. metrics 计数
# =============================================================
class TestMetricsCounters:
    def test_metrics_increments(self, db, alice):
        # 清零相关计数
        before_propose = metrics.confirm_ticket_propose_total
        before_exec = metrics.confirm_ticket_executed_total
        before_rej = metrics.confirm_ticket_rejected_total
        ct.register_executor("refund_execute",
                               lambda a, u: {"status": "ok"})
        t1 = ct.propose(db, tool_name="refund_execute",
                        args={"order_no": "O1"}, summary="s", user_id=alice.id)
        t2 = ct.propose(db, tool_name="refund_execute",
                        args={"order_no": "O2"}, summary="s", user_id=alice.id)
        assert metrics.confirm_ticket_propose_total == before_propose + 2
        assert metrics.confirm_ticket_by_tool.get("refund_execute") is not None
        ct.confirm(db, t1["id"], user_id=alice.id)
        assert metrics.confirm_ticket_executed_total == before_exec + 1
        ct.reject(db, t2["id"], user_id=alice.id, reason="改主意")
        assert metrics.confirm_ticket_rejected_total == before_rej + 1


# =============================================================
# 6. args_hash 稳定性（顺序无关）
# =============================================================
class TestArgsHashStability:
    def test_dict_order_doesnt_change_hash(self):
        h1 = ct.args_hash({"a": 1, "b": 2})
        h2 = ct.args_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_nested_dict_order_doesnt_change_hash(self):
        h1 = ct.args_hash({"x": {"a": 1, "b": 2}})
        h2 = ct.args_hash({"x": {"b": 2, "a": 1}})
        assert h1 == h2


# =============================================================
# 7. API 端点 RBAC（propose 不允许匿名）
# =============================================================
class TestAPIRBAC:
    """FastAPI 点对点：未登录用户不能访问 /api/confirm/tickets"""
    def test_list_unauthenticated_401(self, db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.confirm_ticket import router
        from app.clients.mysql_client import get_db

        app = FastAPI()
        app.include_router(router)
        # 覆盖 get_db 为 sqlite 内存 session（其他 dep 走正常链），
        # 401/403 由 get_current_user 内层 HTTPException 抛出
        app.dependency_overrides[get_db] = lambda: db
        c = TestClient(app)
        r = c.get("/api/confirm/tickets")
        assert r.status_code in (401, 403)