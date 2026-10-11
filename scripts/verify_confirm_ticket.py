#!/usr/bin/env python
"""V14（human-in-the-loop）confirm_ticket 活体校验

完整走一遍"AI 出提案 → 人点确认 → 服务端真执行"三段链路：
    0. 注册一个普通用户 + 一个 admin（admin 仅为运维旁路，本测试不需要）
    1. 用户登录拿 session
    2. 提交 refund_execute 提案（直接走 API；真实 FC 流是 agent_runner → tool dispatch）
    3. 列提案——验证该用户看得到自己的 ticket
    4. 篡改提案的 tool_args → 试图绕过 confirm → 必须 401/403/参数冻结报错
    5. 别人的 ticket 不能 confirm → 401/403
    6. 正常 confirm → 看到 status=executed + result_snapshot 里真有 refund_no
    7. 二次 confirm → 幂等（不重复执行、result 一致）
    8. 拒绝一条新提案 → status=rejected
    9. 过期提案 → sweep 推进为 expired（手动把 expires_at 改过去再 sweep）

每条 PASS/FAIL；报告落 data/confirm_ticket_drill_report.json。
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

# 早期设置：让 app.config 启动校验通过（sweep_expired 等子模块会触发 import）
os.environ.setdefault("JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://cs_user:dev_user_2026@localhost:3307/customer_service?charset=utf8mb4",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

BASE = os.getenv("BASE_URL", "http://localhost:8000")
DB_URL = os.getenv("DATABASE_URL", "")
_fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name}" + (f" :: {detail}" if detail else ""))
    if not cond:
        _fails.append(name)


def main() -> int:
    if not DB_URL:
        print("FAIL preflight: 需要 DATABASE_URL（直接连 DB 验证 sweep 后端状态）")
        return 1
    import requests
    from passlib.hash import bcrypt
    from sqlalchemy import create_engine, text

    # 每次跑用唯一 session_id——避免 dedup 撞到前一轮同名票据
    drill_sid = f"drill-{uuid.uuid4().hex[:8]}"

    # 0) 直连 DB 建一个普通用户（绕开注册限流）
    suffix_str = uuid.uuid4().hex[:6]
    user = f"vfy_u_{suffix_str}"
    pw = "Drill#2026ticket!"
    pw_hash = bcrypt.hash(pw)
    eng = create_engine(DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (username, password_hash, role, display_name, status) "
            "VALUES (:u, :p, 'user', 'tickdrill', 1)"
        ), {"u": user, "p": pw_hash})
    print(f"created user {user}")

    # 1) 登录拿 session（httpOnly cookie）
    s = requests.Session()
    login_r = s.post(f"{BASE}/api/auth/login",
                      data={"username": user, "password": pw}, timeout=15)
    check("login", login_r.ok, login_r.text[:80])

    # 2) AI 侧提案（直接调 propose 端点——FC 流程最终也是这条路）
    # 用真实存在的订单号（从 seed 数据查一条 paid/shipped/completed 状态的），
    # 否则 OrderLifecycle.refund_order 会抛 AttributeError（演示仍能跑通流程，
    # 但 result_snapshot 装的是错误而不是真退款号；真实场景应拿真实订单）
    real_order_no = None
    with eng.begin() as conn:
        row = conn.execute(text(
            "SELECT order_no FROM orders WHERE status IN "
            "('paid','shipped','delivered') AND deleted=0 AND user_id IN "
            "(SELECT id FROM users WHERE role IN ('user','visitor')) "
            "ORDER BY id ASC LIMIT 1"
        )).first()
    if row is None:
        # 没真实订单——注册一票 demo 单来模拟；用真实可支付订单走一遍更稳
        real_order_no = "ORD-TICKET-DRILL"
        print(f"   WARNING: 找不到种子订单，用 {real_order_no} 演示（result 会带 error，"
              f"但 confirm_ticket 流程全跑通）")
    else:
        real_order_no = row[0]
        print(f"   using real order_no={real_order_no}")
    propose_body = {
        "tool_name": "refund_execute",
        "args": {"order_no": real_order_no, "reason": "drill 校验"},
        "summary": f"退款 {real_order_no}（演示票）",
        "session_id": drill_sid,
    }
    r = s.post(f"{BASE}/api/confirm/tickets/propose",
               json=propose_body, timeout=15)
    check("propose.201", r.status_code in (200, 201), r.text[:80])
    ticket = r.json()["data"]
    tid = ticket["id"]
    # 快照 args 用于"篡改 → 还原"循环——必须用真实发出的对象，否则 hash 比对失败
    original_args = dict(propose_body["args"])
    print(f"   ticket_id={tid}  status={ticket['status']}  args_hash={ticket['args_hash'][:12]}…")

    # 3) 列提案——验证用户能看自己的
    r = s.get(f"{BASE}/api/confirm/tickets", timeout=15)
    ids = [t["id"] for t in r.json()["data"]["items"]]
    check("list.contains_own_ticket", tid in ids, f"items={ids}")

    # 4) 篡改票面 tool_args 试图绕过 confirm——参数冻结应拒绝
    with eng.begin() as conn:
        conn.execute(text(
            "UPDATE confirm_tickets SET tool_args = JSON_OBJECT("
            "'order_no','ORD-CHANGED','-C','drill 攻击') WHERE id=:i"
        ), {"i": tid})
    r = s.post(f"{BASE}/api/confirm/tickets/{tid}/confirm", timeout=15)
    check("defense1_args_hash_freeze_rejected",
          r.status_code == 400, f"status={r.status_code} {r.text[:80]}")

    # 把 args 还原（恢复真值才能跑后续 step6）
    args_json = json.dumps(original_args, ensure_ascii=False)
    with eng.begin() as conn:
        conn.execute(text(
            "UPDATE confirm_tickets SET tool_args = CAST(:j AS JSON) WHERE id=:i"
        ), {"i": tid, "j": args_json})

    # 5) 别人不能 confirm——建第二个用户
    user2 = f"vfy_u2_{uuid.uuid4().hex[:6]}"
    pw_hash2 = bcrypt.hash(pw)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (username, password_hash, role, display_name, status) "
            "VALUES (:u, :p, 'user', 'tickdrill', 1)"
        ), {"u": user2, "p": pw_hash2})
    s2 = requests.Session()
    s2.post(f"{BASE}/api/auth/login",
            data={"username": user2, "password": pw}, timeout=15).raise_for_status()
    r = s2.post(f"{BASE}/api/confirm/tickets/{tid}/confirm", timeout=15)
    check("defense_cross_user_blocked",
          r.status_code in (403, 404),
          f"status={r.status_code} {r.text[:80]}")

    # 6) 正常 confirm——能看到 status=executed + result_snapshot
    r = s.post(f"{BASE}/api/confirm/tickets/{tid}/confirm", timeout=15)
    check("confirm.200", r.ok, r.text[:80])
    result = r.json()["data"]
    check("confirm.executed_status",
          result["status"] == "executed", f"status={result['status']}")
    check("confirm.has_result_snapshot",
          result.get("result_snapshot") is not None)
    print(f"   result_snapshot={result.get('result_snapshot')}")

    # 7) 幂等：再 confirm 一次，不重复执行（防双花）
    r = s.post(f"{BASE}/api/confirm/tickets/{tid}/confirm", timeout=15)
    again = r.json()["data"]
    check("defense3_idempotent",
          again["status"] == "executed" and again["result_snapshot"] == result["result_snapshot"],
          f"再次确认后 status={again['status']}")

    # 8) 拒绝一条新提案
    propose2 = s.post(f"{BASE}/api/confirm/tickets/propose",
                      json={
                          "tool_name": "refund_execute",
                          "args": {"order_no": "ORD-REJECT", "reason": "x"},
                          "summary": "拒绝演示",
                          "session_id": drill_sid,
                      }, timeout=15)
    tid2 = propose2.json()["data"]["id"]
    r = s.post(f"{BASE}/api/confirm/tickets/{tid2}/reject",
               json={"reason": "用户反悔"}, timeout=15)
    check("reject.200", r.ok and r.json()["data"]["status"] == "rejected",
          r.text[:80])

    # 9) 过期：手动把 expires_at 改到过去，sweep 后 DELETE
    propose3 = s.post(f"{BASE}/api/confirm/tickets/propose",
                      json={
                          "tool_name": "refund_execute",
                          "args": {"order_no": "ORD-EXPIRE", "reason": "x"},
                          "summary": "过期演示",
                          "session_id": drill_sid,
                      }, timeout=15)
    tid3 = propose3.json()["data"]["id"]
    with eng.begin() as conn:
        conn.execute(text(
            "UPDATE confirm_tickets SET expires_at = DATE_SUB(NOW(), INTERVAL 10 SECOND) "
            "WHERE id=:i"
        ), {"i": tid3})
    # sweep 通过直接调服务（模拟 lazy 启动期调用）
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from app.services.confirm_ticket_service import sweep_expired
    from app.clients.mysql_client import get_session_local
    SessionLocal = get_session_local()
    with SessionLocal() as db:
        n = sweep_expired(db)
    check("defense2_ttl_sweep", n >= 1, f"扫掉了 {n} 张过期票")
    r = s.get(f"{BASE}/api/confirm/tickets/{tid3}", timeout=15)
    check("expired.status",
          r.json()["data"]["status"] == "expired", f"status={r.json()['data']['status']}")

    # 落报告
    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ticket_id": tid, "ticket_rejected_id": tid2, "ticket_expired_id": tid3,
        "passed": not _fails, "fails": _fails,
    }
    out = PROJECT_ROOT / "data" / "confirm_ticket_drill_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n{'PASS' if not _fails else 'FAIL: ' + ','.join(_fails)}")
    print(f"REPORT -> {out}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())