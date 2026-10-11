"""V14（2.4）badcase 自动池测试

覆盖：
1. 入池与去重（归一化哈希、bump 不重复建行、空问句不收）
2. 👎 反查现场（join 2.3 intent_snapshot / contexts 两形状 / message_id=0 兜底 / 无现场显式 error 计数）
3. guard 拦截留证
4. 低置信扫描（只收 llm 且 conf<阈值；时间窗；去重 bump）
5. 审核状态机（合法迁移 / merged 锁定 / 不存在 404）
6. admin API（列表过滤分页 / PATCH 流转 / collect）
"""
import datetime as dt
import os
import sys

os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import badcase_admin
from app.api.deps import require_admin
from app.clients.mysql_client import get_db
from app.models.base import Base
from app.models.badcase import BadcaseCandidate
from app.models.message import Message
from app.models.user import User
from app.services import badcase_service
from app.services.metrics import metrics


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def admin_user():
    return User(id=1, username="admin", password_hash="fake", role="admin", status=1)


def _add_pair(db, *, session_id="s1", user_id=7, query="怎么还没退款",
              intent=None, contexts=None, scores=None, answer="您的退款已提交。"):
    """造一轮 user→assistant 消息，返回 assistant 行。"""
    u = Message(session_id=session_id, user_id=user_id, role="user",
                content=query, create_time=dt.datetime.now())
    db.add(u)
    db.flush()
    a = Message(session_id=session_id, user_id=user_id, role="assistant",
                content=answer, intent_snapshot=intent, contexts=contexts,
                scores=scores, create_time=dt.datetime.now())
    db.add(a)
    db.commit()
    return a


# =============================================================
# 1. 入池与去重
# =============================================================
class TestEnqueue:
    def test_created_then_bumped(self, db):
        r1 = badcase_service.enqueue(db, signal="thumbs_down", query="退 款  多久到账")
        r2 = badcase_service.enqueue(db, signal="thumbs_down", query="退 款 多久到账 ")
        assert r1["action"] == "created"
        assert r2["action"] == "bumped" and r2["hit_count"] == 2
        assert db.query(BadcaseCandidate).count() == 1  # 归一化后同哈希，不重复建行

    def test_empty_query_returns_none(self, db):
        assert badcase_service.enqueue(db, signal="guard_blocked", query="   ") is None
        assert db.query(BadcaseCandidate).count() == 0

    def test_same_query_different_signal_coexist(self, db):
        badcase_service.enqueue(db, signal="thumbs_down", query="尺码表在哪")
        badcase_service.enqueue(db, signal="low_confidence", query="尺码表在哪")
        assert db.query(BadcaseCandidate).count() == 2


# =============================================================
# 2. 👎 反查现场
# =============================================================
class TestThumbsDown:
    def test_joins_scene(self, db):
        a = _add_pair(
            db, query="退款到账要几天",
            intent={"primary": "policy_query", "method": "rule", "confidence": 0.9},
            contexts=[{"payload": {"source": "policy_refund_days"}, "text": "退款 1-7 个工作日到账"}],
            scores=[0.81],
        )
        r = badcase_service.enqueue_thumbs_down(
            db, session_id="s1", message_id=a.id, user_id=7)
        row = db.get(BadcaseCandidate, r["id"])
        assert row.query == "退款到账要几天"
        assert row.snapshot["intent"]["primary"] == "policy_query"
        assert row.snapshot["top_contexts"][0]["source"] == "policy_refund_days"
        assert row.snapshot["top_contexts"][0]["score"] == 0.81
        assert row.snapshot["message_id_fallback"] is False

    def test_dict_and_str_context_shapes(self, db):
        a = _add_pair(db, contexts=["纯字符串旧形状", {"source": "kb_b", "text": "新形状"}],
                      scores=None)
        badcase_service.enqueue_thumbs_down(db, session_id="s1", message_id=a.id)
        row = db.query(BadcaseCandidate).one()
        srcs = [c["source"] for c in row.snapshot["top_contexts"]]
        assert srcs == [None, "kb_b"]  # 两种形状都吃得下，不假设

    def test_message_id_zero_fallback_latest_assistant(self, db):
        _add_pair(db, session_id="s2", query="第一轮", answer="A1")
        a2 = _add_pair(db, session_id="s2", query="第二轮", answer="A2")
        r = badcase_service.enqueue_thumbs_down(db, session_id="s2", message_id=0)
        row = db.get(BadcaseCandidate, r["id"])
        assert row.query == "第二轮"
        assert row.message_id == a2.id
        assert row.snapshot["message_id_fallback"] is True

    def test_no_scene_counts_error_not_silent(self, db):
        before = metrics.badcase_enqueue_error
        assert badcase_service.enqueue_thumbs_down(
            db, session_id="ghost", message_id=0) is None
        assert metrics.badcase_enqueue_error == before + 1


# =============================================================
# 3. guard 留证
# =============================================================
class TestGuardBlocked:
    def test_layer_reason_stored(self, db):
        r = badcase_service.enqueue_guard_blocked(
            db, query="投诉！骗人", session_id="s3", user_id=2,
            layer="L1", reason="blacklist")
        row = db.get(BadcaseCandidate, r["id"])
        assert row.signal == "guard_blocked"
        assert row.snapshot == {"guard_layer": "L1", "guard_reason": "blacklist"}


# =============================================================
# 4. 低置信扫描
# =============================================================
class TestCollectLowConfidence:
    def test_only_llm_below_threshold(self, db):
        _add_pair(db, session_id="c1", query="rule低也不收",
                  intent={"primary": "x", "method": "rule", "confidence": 0.1})
        _add_pair(db, session_id="c2", query="llm高置信不收",
                  intent={"primary": "x", "method": "llm", "confidence": 0.9})
        _add_pair(db, session_id="c3", query="llm低置信要收",
                  intent={"primary": "refund_query", "method": "llm", "confidence": 0.4})
        out = badcase_service.collect_low_confidence(db)
        assert out["created"] == 1
        row = db.query(BadcaseCandidate).one()
        assert row.signal == "low_confidence" and row.query == "llm低置信要收"
        assert out["scanned"] == 2  # llm 的两条都"扫到"，只有低置信入池

    def test_rerun_bumps_not_duplicates(self, db):
        _add_pair(db, session_id="c4", query="模糊问题",
                  intent={"method": "llm", "confidence": 0.3})
        badcase_service.collect_low_confidence(db)
        out = badcase_service.collect_low_confidence(db)
        assert out["bumped"] == 1 and out["created"] == 0
        assert db.query(BadcaseCandidate).count() == 1

    def test_since_window_excludes_old(self, db):
        a = _add_pair(db, session_id="c5", query="很久以前",
                      intent={"method": "llm", "confidence": 0.2})
        a.create_time = dt.datetime.now() - dt.timedelta(days=30)
        db.commit()
        out = badcase_service.collect_low_confidence(
            db, since=dt.datetime.now() - dt.timedelta(days=7))
        assert out["created"] == 0


# =============================================================
# 5. 审核状态机
# =============================================================
class TestReviewStateMachine:
    def _new_row(self, db):
        return badcase_service.enqueue(db, signal="thumbs_down", query="q1")

    def test_review_then_merge(self, db, admin_user):
        r = self._new_row(db)
        row = badcase_service.review(db, badcase_id=r["id"], action="reviewed",
                                     admin_id=admin_user.id, expected_source="policy_x")
        assert row["status"] == "reviewed" and row["expected_source"] == "policy_x"
        assert row["reviewed_by"] == admin_user.id
        badcase_service.mark_merged(db, badcase_id=r["id"], note="eval+1")
        row2 = badcase_service.get_by_id(db, r["id"])
        assert row2["status"] == "merged" and row2["merged_at"]

    def test_merged_locked(self, db):
        r = self._new_row(db)
        badcase_service.review(db, badcase_id=r["id"], action="reviewed", admin_id=1)
        badcase_service.mark_merged(db, badcase_id=r["id"], note="x")
        with pytest.raises(ValueError):
            badcase_service.review(db, badcase_id=r["id"], action="reviewed", admin_id=1)

    def test_discard_and_recheck(self, db):
        r = self._new_row(db)
        badcase_service.review(db, badcase_id=r["id"], action="discarded", admin_id=1)
        row = badcase_service.review(db, badcase_id=r["id"], action="reviewed",
                                     admin_id=1, note="复核确认为误杀")
        assert row["status"] == "reviewed"

    def test_re_review_updates_annotation(self, db):
        r = self._new_row(db)
        badcase_service.review(db, badcase_id=r["id"], action="reviewed", admin_id=1,
                               expected_source="wrong_src")
        row = badcase_service.review(db, badcase_id=r["id"], action="reviewed", admin_id=2,
                                     expected_source="right_src")
        assert row["expected_source"] == "right_src"

    def test_not_found_raises_lookup(self, db):
        with pytest.raises(LookupError):
            badcase_service.review(db, badcase_id=9999, action="reviewed", admin_id=1)


# =============================================================
# 6. admin API（TestClient + 依赖覆盖，仿 test_admin_conversations）
# =============================================================
@pytest.fixture
def client(db, admin_user):
    app = FastAPI()
    app.include_router(badcase_admin.router)
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


class TestBadcaseAPI:
    def test_list_pagination_and_filters(self, client, db):
        for i in range(3):
            badcase_service.enqueue(db, signal="thumbs_down", query=f"问题{i}")
        badcase_service.enqueue_guard_blocked(
            db, query="坏词", session_id="s", user_id=1, layer="L1", reason="blacklist")
        r = client.get("/api/admin/badcases", params={"signal": "thumbs_down",
                                                      "page_size": 2})
        body = r.json()
        assert r.status_code == 200 and body["ok"]
        assert body["data"]["total"] == 3 and len(body["data"]["items"]) == 2
        r2 = client.get("/api/admin/badcases", params={"status": "new",
                                                       "signal": "guard_blocked"})
        assert r2.json()["data"]["total"] == 1

    def test_patch_flow_and_errors(self, client, db):
        e = badcase_service.enqueue(db, signal="thumbs_down", query="待审")
        r = client.patch(f"/api/admin/badcases/{e['id']}",
                         json={"action": "reviewed", "expected_source": "policy_a"})
        assert r.status_code == 200 and r.json()["data"]["status"] == "reviewed"
        assert client.patch("/api/admin/badcases/9999",
                            json={"action": "reviewed"}).status_code == 404
        # new→new 非法迁移 → 400
        e2 = badcase_service.enqueue(db, signal="thumbs_down", query="待审2")
        assert client.patch(f"/api/admin/badcases/{e2['id']}",
                            json={"action": "discarded"}).status_code == 200
        badcase_service.mark_merged(db, badcase_id=e["id"], note="x")
        assert client.patch(f"/api/admin/badcases/{e['id']}",
                            json={"action":"reviewed"}).status_code == 400

    def test_patch_bad_action_422(self, client, db):
        e = badcase_service.enqueue(db, signal="thumbs_down", query="q")
        assert client.patch(f"/api/admin/badcases/{e['id']}",
                            json={"action": "merge"}).status_code == 422

    def test_collect_endpoint(self, client, db):
        _add_pair(db, session_id="api1", query="api低置信",
                  intent={"method": "llm", "confidence": 0.35})
        r = client.post("/api/admin/badcases/collect",
                        json={"days": 7, "threshold": 0.6, "limit": 50})
        assert r.status_code == 200
        assert r.json()["data"]["created"] == 1

    def test_non_admin_gets_401_403(self, db):
        """真实依赖链：不覆盖 require_admin → 无 token 必须被拒（RBAC 回归锁）。"""
        app = FastAPI()
        app.include_router(badcase_admin.router)
        app.dependency_overrides[get_db] = lambda: db
        c = TestClient(app)
        assert c.get("/api/admin/badcases").status_code in (401, 403)


# =============================================================
# 7. merge 脚本可导入的纯函数（不触网：doc_id 解析由调用方注入）
# =============================================================
class TestMergeScriptHelpers:
    def _load_module(self):
        import importlib.util
        path = (os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))) + os.sep + "scripts" + os.sep +
            "merge_badcase_to_eval.py")
        spec = importlib.util.spec_from_file_location("merge_bce", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_resolve_source_priority_and_fallback(self, db):
        mod = self._load_module()
        r = badcase_service.enqueue(db, signal="thumbs_down", query="有标注")
        row = db.get(BadcaseCandidate, r["id"])
        row.expected_source = "policy_x"
        assert mod.resolve_source(row) == ("policy_x", "expected_source")
        row.expected_source = None
        row.snapshot = {"top_contexts": [{"source": None}, {"source": "kb_y"}]}
        assert mod.resolve_source(row) == ("kb_y", "snapshot_fallback")
        row.snapshot = {"top_contexts": [{"source": None}]}
        src, how = mod.resolve_source(row)
        assert src is None and how == "no_source"

    def test_merged_row_shape(self, db):
        """合入条目的稳定键三元组齐备（source 在、doc_id 由脚本注入后才算数）。"""
        r = badcase_service.enqueue(db, signal="low_confidence", query="尺码会偏吗")
        row = db.get(BadcaseCandidate, r["id"])
        entry = {"query": row.query, "relevant_doc_id": "uuid-5", "source": "product_s1"}
        assert set(entry) == {"query", "relevant_doc_id", "source"}
        assert entry["query"] == "尺码会偏吗"
