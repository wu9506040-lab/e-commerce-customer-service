#!/usr/bin/env python
"""V14（2.4）飞轮闭环活体校验：真实问答 → 👎/guard 双信号 → 池 → 审核

一次跑通采集侧全链路（合入侧由 merge_badcase_to_eval.py + 单测覆盖）：
    register → SQL 提权 admin → /api/chat 真实一轮 → 👎 → 英文问句 guard 误杀
    → admin 池必须同时见 thumbs_down(带现场) 与 guard_blocked → reviewed 流转
    → collect 端点可用。每步 PASS/FAIL，任一失败退出码非 0。

前置：docker compose 五服务在跑（含 LLM/Qdrant 真实依赖）。
用法：
    DATABASE_URL='mysql+pymysql://cs_user:<pwd>@localhost:3307/customer_service?charset=utf8mb4' \
    [BASE_URL=http://localhost:8000] [QDRANT_URL=http://localhost:6333] \
    PYTHONIOENCODING=utf-8 python scripts/verify_flywheel_live.py
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

BASE = os.getenv("BASE_URL", "http://localhost:8000")
QDRANT = os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/")
DB_URL = os.getenv("DATABASE_URL")
PW = "Fly#2026verify!"

_fails = []


def check(name, cond, detail=""):
    line = f"{'PASS' if cond else 'FAIL'} {name}" + (f" :: {detail}" if detail else "")
    print(line)
    if not cond:
        _fails.append(name)


def main() -> int:
    import requests
    if not DB_URL:
        print("FAIL preflight: 需要 DATABASE_URL（提权 admin 用）")
        return 1
    tag = uuid.uuid4().hex[:6]
    u_name, a_name = f"vfy_u_{tag}", f"vfy_a_{tag}"
    su, sa = requests.Session(), requests.Session()

    for n in (u_name, a_name):
        r = su.post(f"{BASE}/api/auth/register",
                    json={"username": n, "password": PW, "display_name": "飞轮校验"},
                    timeout=15)
        check(f"register {n}", r.status_code in (200, 201), r.text[:80])
    if _fails:
        return 1

    # SQL 提权（直连，容器/宿主机同一 DATABASE_URL 语义）
    from sqlalchemy import create_engine, text
    eng = create_engine(DB_URL, pool_pre_ping=True)
    with eng.begin() as conn:
        conn.execute(text("UPDATE users SET role='admin' WHERE username=:n"),
                     {"n": a_name})
    check("promote-admin", True)

    r = su.post(f"{BASE}/api/auth/login", data={"username": u_name, "password": PW},
                timeout=15)
    r2 = sa.post(f"{BASE}/api/auth/login", data={"username": a_name, "password": PW},
                 timeout=15)
    check("login both", r.ok and r2.ok)

    # 1) 真实问答一轮
    r = su.post(f"{BASE}/api/chat", stream=True, timeout=180,
                json={"query": f"怎么还没退款啊，一般几天到账？（校验{tag}）"})
    session_id, aid = None, None
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "done":
                session_id = ev.get("session_id") or session_id
                aid = ev.get("assistant_message_id")
    check("chat-round", bool(aid), f"session={session_id} msg_id={aid}")
    if not aid:
        return 1

    # 2) 👎 入池
    r = su.post(f"{BASE}/api/chat/rate", timeout=15,
                json={"session_id": session_id, "message_id": aid,
                      "rating": "down", "comment": "校验：答非所问"})
    check("rate-down", r.ok, str(r.json())[:80])

    # 3) guard 误杀入池（英文正当问句被 L1 拦）
    r = su.post(f"{BASE}/api/chat", stream=True, timeout=60,
                json={"query": f"how to check my refund status please {tag}",
                      "session_id": session_id})
    blocked = None
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "meta" and ev.get("intent") == "blocked":
                blocked = (ev.get("guard_layer"), ev.get("guard_reason"))
                break
    check("guard-blocked-signal", blocked is not None, str(blocked))

    # 4) 池内双信号齐 + 现场完整
    time.sleep(0.5)
    rows = sa.get(f"{BASE}/api/admin/badcases", timeout=15,
                params={"status": "new"}).json()["data"]["items"]
    td = next((x for x in rows if x["signal"] == "thumbs_down"), None)
    gb = next((x for x in rows if x["signal"] == "guard_blocked"), None)
    check("pool-both-signals", bool(td and gb),
          f"thumbs={bool(td)} guard={bool(gb)}")
    if td:
        snap = td.get("snapshot") or {}
        scene_ok = ("intent" in snap and "top_contexts" in snap
                    and snap.get("answer_excerpt"))
        check("thumbs-down-scene", scene_ok, f"keys={sorted(snap)}")
        # 5) reviewed 流转（标注稳定键：现场 source，缺省回退现库任取一源）
        src = next((c["source"] for c in snap.get("top_contexts") or []
                    if c.get("source")), None)
        if src is None:
            sc = requests.post(f"{QDRANT}/collections/knowledge_base/points/scroll",
                               json={"limit": 1, "with_payload": ["source"]},
                               timeout=10).json()
            src = sc["result"]["points"][0]["payload"]["source"]
        r = sa.patch(f"{BASE}/api/admin/badcases/{td['id']}", timeout=15,
                     json={"action": "reviewed", "expected_source": src})
        check("review-transition",
              r.ok and r.json()["data"]["status"] == "reviewed", f"src={src}")
    # 6) collect 低置信扫描端点可用
    r = sa.post(f"{BASE}/api/admin/badcases/collect", timeout=60,
                json={"days": 1, "threshold": 0.6, "limit": 100})
    check("collect-endpoint", r.ok, str(r.json().get("data")))
    # 清理 guard 演示行状态不强求，池数据保留作证据（data/ 不进 git，DB 行无妨）

    print(f"SUMMARY {'PASS' if not _fails else 'FAIL: ' + ','.join(_fails)}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
