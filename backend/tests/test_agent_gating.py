"""审批门控测试（V20 亮点 2：ApprovalGate 确认票据 / 幂等 / 审计）"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.agent_gating import (
    ApprovalGate,
    get_approval_gate,
    reset_approval_gate,
)

PAYLOAD = {"order_no": "ORD9", "new_address": "东莞市松山湖 1 号"}


@pytest.fixture(autouse=True)
def _fresh(tmp_path: Path):
    reset_approval_gate()
    yield
    reset_approval_gate()


def test_request_creates_pending_ticket(tmp_path):
    g = ApprovalGate(audit_dir=str(tmp_path))
    t = g.request("order_modify.address", 7, PAYLOAD)
    assert t["status"] == "pending"
    assert t["tool"] == "order_modify.address"
    assert t["ticket_id"]


def test_idempotent_request_same_ticket(tmp_path):
    g = ApprovalGate(audit_dir=str(tmp_path))
    t1 = g.request("order_modify.address", 7, PAYLOAD)
    t2 = g.request("order_modify.address", 7, PAYLOAD)  # 同 payload 再申请
    assert t1["ticket_id"] == t2["ticket_id"]           # 幂等：同一张票


def test_verify_consumes_then_duplicate_rejected(tmp_path):
    g = ApprovalGate(audit_dir=str(tmp_path))
    g.request("order_modify.address", 7, PAYLOAD)

    ok, reason = g.verify_and_consume("order_modify.address", 7, PAYLOAD)
    assert ok is True and reason == ""

    ok2, reason2 = g.verify_and_consume("order_modify.address", 7, PAYLOAD)
    assert ok2 is False and reason2 == "duplicate"      # 幂等拒绝二次执行


def test_no_ticket_rejected(tmp_path):
    """没走确认流程直接 confirmed=True → 拒绝（no_ticket）。"""
    g = ApprovalGate(audit_dir=str(tmp_path))
    ok, reason = g.verify_and_consume("order_modify.address", 7, PAYLOAD)
    assert ok is False and reason == "no_ticket"


def test_expired_ticket_rejected(tmp_path):
    g = ApprovalGate(ttl_seconds=-1, audit_dir=str(tmp_path))  # 立即过期
    g.request("order_modify.address", 7, PAYLOAD)
    ok, reason = g.verify_and_consume("order_modify.address", 7, PAYLOAD)
    assert ok is False and reason == "expired"


def test_payload_change_invalidates_ticket(tmp_path):
    """参数变了（LLM 幻觉改参数）→ 票据不匹配 → no_ticket。"""
    g = ApprovalGate(audit_dir=str(tmp_path))
    g.request("order_modify.address", 7, PAYLOAD)
    ok, reason = g.verify_and_consume(
        "order_modify.address", 7, {**PAYLOAD, "new_address": "被幻觉改掉的地址"},
    )
    assert ok is False and reason == "no_ticket"


def test_reject_blocks_pending(tmp_path):
    g = ApprovalGate(audit_dir=str(tmp_path))
    g.request("order_modify.address", 7, PAYLOAD)
    g.reject("order_modify.address", 7, PAYLOAD)
    ok, reason = g.verify_and_consume("order_modify.address", 7, PAYLOAD)
    assert ok is False and reason == "rejected"


def test_isolates_users_and_tools(tmp_path):
    g = ApprovalGate(audit_dir=str(tmp_path))
    g.request("order_modify.address", 7, PAYLOAD)
    ok, reason = g.verify_and_consume("order_modify.address", 8, PAYLOAD)  # 换用户
    assert ok is False and reason == "no_ticket"
    ok2, reason2 = g.verify_and_consume("order_modify.merge", 7, {"order_nos": ["A", "B"]})
    assert ok2 is False and reason2 == "no_ticket"                        # 换工具


def test_audit_jsonl_written(tmp_path):
    g = ApprovalGate(audit_dir=str(tmp_path))
    g.request("order_modify.address", 7, PAYLOAD)
    g.verify_and_consume("order_modify.address", 7, PAYLOAD)
    audit = (tmp_path / "approvals.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(l)["event"] for l in audit]
    assert "request" in events and "execute" in events


def test_singleton():
    assert get_approval_gate() is get_approval_gate()



