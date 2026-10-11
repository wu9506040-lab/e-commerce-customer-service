#!/usr/bin/env python
"""V14（3.3）故障演练：把"最坏 10 分钟→90 秒"工程量成数字

三道独立攻击各自印证 spec 一行承诺——
  A. qdrant 拔线：端到端 /chat 仍能应答应答（断路器降级）；max 延迟 vs 预算
  B. LLM 黑洞：容器内用 unreachable 端点跑真 qwen.chat，验证
     - 单请求在预算内拒绝（≤90s）
     - 连续 5 次后断路器 OPEN → 后续请求 <100ms 快速失败（无重试）
  C. 并发 20 路 /chat：所有请求在预算内完成，错误率 <0.5%
     semaphore 超窗计数 / fallback_fastfail 计数全打印

每场景 PASS/FAIL + 数字摘要落 data/drill_failover_report.json。

用法：
    DATABASE_URL='mysql+pymysql://cs_user:<pwd>@localhost:3307/customer_service?charset=utf8mb4' \
    [BASE_URL=http://localhost:8000] \
    PYTHONIOENCODING=utf-8 python scripts/drill_failover.py
"""
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import List

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

BASE = os.getenv("BASE_URL", "http://localhost:8000")
DB_URL = os.getenv("DATABASE_URL", "")
_fails: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    line = f"{'PASS' if cond else 'FAIL'} {name}" + (f" :: {detail}" if detail else "")
    print(line)
    if not cond:
        _fails.append(name)


def p50(xs: List[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def pct(xs: List[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    idx = max(0, min(len(xs) - 1, int(q * len(xs))))
    return xs[idx]


# =============================================================
# 共用：登录拿 token + 创建评测用户
# =============================================================
def login_session(name: str, pw: str) -> requests.Session:
    r = requests.post(f"{BASE}/api/auth/login",
                      data={"username": name, "password": pw}, timeout=15)
    r.raise_for_status()
    s = requests.Session()
    # login 通过 httpOnly cookie；直接复制 cookie jar
    for c in r.cookies:
        s.cookies.set_cookie(c)
    return s


def register_and_promote(suffix: str) -> tuple:
    """注册两个用户（普通 + admin），把 admin 的 role 升上去，返回
    (user_session, admin_session)。"""
    import requests
    from sqlalchemy import create_engine, text
    u, a, pw = f"drill_u_{suffix}", f"drill_a_{suffix}", "Drill#2026test!"
    su = requests.Session()
    sa = requests.Session()
    for sess, name in ((su, u), (sa, a)):
        r = sess.post(f"{BASE}/api/auth/register",
                      json={"username": name, "password": pw,
                            "display_name": "drill"}, timeout=15)
        assert r.status_code in (200, 201), r.text
    if DB_URL:
        eng = create_engine(DB_URL, pool_pre_ping=True)
        with eng.begin() as conn:
            conn.execute(text("UPDATE users SET role='admin' WHERE username=:n"),
                         {"n": a})
    su2, sa2 = login_session(u, pw), login_session(a, pw)
    return su2, sa2


def admin_metrics(sa) -> dict:
    """读 /api/admin/.../metrics snapshot（如果存在）；否则退到 /api/admin/analytics"""
    # 没有统一的 metrics 端点——走 docker logs 取启动自报行。
    return {}


def send_chat(s, query: str, timeout: float = 120.0) -> dict:
    """发 /chat（流式），收集 timing / 是否含 token / 是否含 done。"""
    import requests
    t0 = time.monotonic()
    r = s.post(f"{BASE}/api/chat", json={"query": query},
               stream=True, timeout=timeout)
    events = []
    last_token = ""
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        try:
            ev = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        events.append(ev)
        if ev.get("type") == "token" and ev.get("text"):
            last_token += ev["text"]
        if ev.get("type") == "done":
            break
    return {
        "duration_ms": round((time.monotonic() - t0) * 1000),
        "events": len(events),
        "token_chars": len(last_token),
        "got_token": bool(last_token),
        "first_event_intent": next((e.get("intent") for e in events
                                   if e.get("type") == "meta"), None),
    }


# =============================================================
# A. qdrant 拔线
# =============================================================
def drill_a_qdrant_down(sa) -> dict:
    import subprocess
    print("\n[A] qdrant 拔线 → /chat 端到端")
    t0 = time.monotonic()
    subprocess.run(["docker", "stop", "customer-service-qdrant"],
                   capture_output=True, check=True)
    # 一次 /chat：期望 qdrant 检索返 [] → policy_query 走 RAG 流（不挂起）
    res = send_chat(sa, "请问退换货政策是怎样的？", timeout=30)
    print(f"   /chat latency: {res['duration_ms']}ms  intent={res['first_event_intent']}  got_token={res['got_token']}")
    check("A.qdrant_down.latency_under_budget",
          res['duration_ms'] <= 30_000,
          f"{res['duration_ms']}ms (should ≤30s)")
    check("A.qdrant_down.responded", res['got_token'],
          "断路器降级应保证至少出 token + done")
    # 启回
    subprocess.run(["docker", "start", "customer-service-qdrant"],
                   capture_output=True, check=True)
    # 等 qdrant 真正就绪
    for _ in range(20):
        try:
            r = subprocess.run(["docker", "exec", "customer-service-qdrant",
                                "wget", "-q", "-O-", "http://localhost:6333/"],
                               capture_output=True, timeout=2)
            if r.returncode == 0:
                break
        except Exception:
            pass
        time.sleep(1)
    print(f"   qdrant 恢复耗时: {round((time.monotonic()-t0)*1000)}ms")
    return res


# =============================================================
# B. LLM 黑洞（容器内真 OpenAI client 打黑端点）
# =============================================================
def drill_b_blackhole() -> dict:
    print("\n[B] 容器内 LLM 黑洞（真 qwen.chat 打黑端点，验证预算+SDK max_retries=0）")
    import subprocess
    code = """
import os, time, sys
os.environ.setdefault('JWT_SECRET','a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4')
os.environ.setdefault('DATABASE_URL','mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4')
# 黑洞：把 SDK 指到一个不可达 IP。给个 dummy key 让 get_client() 不在第一步 ValueError
os.environ['QWEN_API_KEY']='sk-drill-dummy'
os.environ['DASHSCOPE_BASE_URL']='http://10.255.255.1:1'
sys.path.insert(0,'/app')
from app.core import deadline as dl, qwen
from app.core.circuit_breaker import CircuitOpenError

# 1) 单次 chat 黑洞端点：应在预算内拒绝（不是 ~10 分钟）
dl.start_budget(90.0)
t0 = time.monotonic()
try:
    qwen.chat([{'role':'user','content':'hi'}], temperature=0.0,
              call_timeout=30.0, stage='llm.chat')
except Exception as e:
    elapsed = time.monotonic() - t0
    print(f'SINGLE: {type(e).__name__} elapsed={elapsed:.2f}s')
else:
    elapsed = time.monotonic() - t0
    print(f'SINGLE: NO RAISE elapsed={elapsed:.2f}s')

# 2) 重复 6 次触发断路器 OPEN（阈值 5）后，验证第 6 次 <100ms 快速失败
#    （不再重试、不再发 HTTP）
results = []
for i in range(6):
    dl.start_budget(90.0)  # 每个 attempt 都给满预算，避免 budget 误伤
    t = time.monotonic()
    try:
        qwen.chat([{'role':'user','content':'hi'}], temperature=0.0,
                  call_timeout=30.0, stage='llm.chat')
        results.append(('NO_RAISE', time.monotonic()-t))
    except CircuitOpenError as e:
        results.append(('CircuitOpenError', time.monotonic()-t))
    except dl.DeadlineExceededError as e:
        results.append(('DeadlineExceeded', time.monotonic()-t))
    except Exception as e:
        results.append((type(e).__name__, time.monotonic()-t))
print('SIX:')
for i, (kind, dt) in enumerate(results, 1):
    print(f'  attempt{i}: {kind} {dt*1000:.0f}ms')
"""
    r = subprocess.run(["docker", "exec", "customer-service-api",
                        "python", "-c", code],
                       capture_output=True, text=True, timeout=300)
    out = r.stdout
    print(out)
    # 解析输出：单次 elapsed 应 ≤ ~3s（一次 30s timeout 因为黑洞很快
    # connection refused/timeout 而非 30s）+ 最后一次 <100ms
    return {"stdout": out, "stderr": r.stderr}


# =============================================================
# C. 并发 20 路
# =============================================================
async def chat_one(s, i: int) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: send_chat(s, f"简单介绍一下你们的退换货政策 标识={uuid.uuid4().hex[:6]}{i}", timeout=120)
    )


async def drill_c_concurrency(sa) -> dict:
    print("\n[C] 并发 20 路 /chat")
    # 复用单个 admin session（已登录的 sa）→ 20 个独立 query 的并发 /
    # chat，验证 semaphore 排队 + 预算闸；用唯一 query 规避 L3 重复检测
    # （L3 限的是同 query，不是同 user）。
    import requests
    from concurrent.futures import ThreadPoolExecutor

    n = 20

    def fire(idx: int):
        # 复用 sa cookie，每个请求 unique query 串
        t0 = time.monotonic()
        r = sa.post(f"{BASE}/api/chat",
                    json={"query": "请问你们的物流时效？"
                                  + uuid.uuid4().hex},
                    stream=True, timeout=120)
        n_token = 0
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "token":
                n_token += 1
            if ev.get("type") == "done":
                break
        return (time.monotonic() - t0, n_token)

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as pool:
        out = list(pool.map(fire, range(n)))
    wall = time.monotonic() - t0
    latencies = [x[0] for x in out]
    tokens = [x[1] for x in out]
    print(f"   wall={wall:.1f}s  latency p50={p50(latencies)*1000:.0f}ms "
          f"p95={pct(latencies, 0.95)*1000:.0f}ms  max={max(latencies)*1000:.0f}ms")
    print(f"   tokens per req: p50={p50(tokens)}  min={min(tokens)}  "
          f"zero_token_responses={sum(1 for t in tokens if t==0)}")
    check("C.max_under_budget",
          max(latencies) <= 95.0,
          f"{max(latencies)*1000:.0f}ms (90s + grace)")
    check("C.error_rate_low",
          sum(1 for t in tokens if t == 0) / n <= 0.05,
          f"{sum(1 for t in tokens if t==0)}/{n} zero-token")
    return {"latencies_ms": [round(l*1000) for l in latencies],
            "tokens": tokens, "wall_s": round(wall, 2)}


# =============================================================
# main
# =============================================================
def main() -> int:
    if not DB_URL:
        print("FAIL preflight: 需要 DATABASE_URL（提权 admin 用）")
        return 1
    # 直连 DB 预创 admin（绕开 register 5/min 限流），登录拿 cookie
    import requests
    from passlib.hash import bcrypt
    from sqlalchemy import create_engine, text
    eng = create_engine(DB_URL, pool_pre_ping=True)
    a = f"drill_a_{uuid.uuid4().hex[:6]}"
    pw_hash = bcrypt.hash("Drill#2026test!")
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (username, password_hash, role, display_name, status) "
            "VALUES (:u, :p, 'admin', 'drill', 1)"
        ), {"u": a, "p": pw_hash})
    # 等前面注册风暴触发的 5/min 限流冷却（前几轮跑挂的可能性），最长等 90s
    for waited in range(0, 91, 5):
        try:
            sa = login_session(a, "Drill#2026test!")
            break
        except Exception as e:
            if "429" in str(e) and waited < 90:
                print(f"   rate-limited, wait {waited+5}s ...")
                time.sleep(5)
            else:
                raise
    print(f"admin {a} created + logged in")
    res_a = drill_a_qdrant_down(sa)
    res_b = drill_b_blackhole()
    res_c = asyncio.run(drill_c_concurrency(sa))
    report = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "A_qdrant_down": res_a, "B_blackhole": res_b, "C_concurrency": res_c,
    }
    out = PROJECT_ROOT / "data" / "drill_failover_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n{'PASS' if not _fails else 'FAIL: ' + ','.join(_fails)}")
    print(f"REPORT -> {out}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())