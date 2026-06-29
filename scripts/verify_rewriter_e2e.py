"""
M12 query_rewriter e2e 验证（httpx 调 /chat + /metrics）

流程：
  1. 登录 demotest 拿 cookie
  2. 拿 metrics snapshot（before）
  3. 多轮对话：
     - Round 1: "iPhone 15 Pro 有什么颜色"（无指代 → skipped_no_coref 计数 +1）
     - Round 2: "它能便宜点吗"（含指代 + 有 history → rewritten 计数 +1）
  4. 拿 metrics snapshot（after）
  5. 验证 by_reason 增量

用法：
    # 先启服务
    cd deploy && docker compose --env-file ../.env.dev up -d
    # 等服务起来后跑
    python scripts/verify_rewriter_e2e.py
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

BASE = "http://localhost:8000"
USERNAME = "demotest"
PASSWORD = "demotest123"
USER_ID = 7  # demotest 的 ID
REDIS_CONTAINER = "customer-service-redis"

PASS = "[PASS]"
FAIL = "[FAIL]"


def clear_rcache_for_user(user_id: int) -> None:
    """清 rcache exact + sem keys（仅限指定 user，不影响 history/session）

    为什么需要：response_cache 的 L2 sem cache 用 embedding 相似度（0.95 阈值），
    即使 query 加 unique tag，sem embedding 仍可能跟历史 query 相似度 > 0.95 命中。
    """
    pattern = f"rcache:*:{user_id}:*"
    cmd = [
        "docker", "exec", REDIS_CONTAINER,
        "sh", "-c",
        f"redis-cli --scan --pattern '{pattern}' | xargs -r redis-cli del > /dev/null",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        print(f"  [WARN] clear_rcache 退出码={r.returncode} stderr={r.stderr[:200]}")


def _print_step(name: str, ok: bool, msg: str = ""):
    mark = PASS if ok else FAIL
    line = f"  {mark} {name}"
    if msg:
        line += f"  ({msg})"
    print(line)
    return ok


async def login(client: httpx.AsyncClient) -> bool:
    """登录 demotest 拿 cookie"""
    r = await client.post(
        f"{BASE}/api/auth/login",
        data={"username": USERNAME, "password": PASSWORD},
    )
    return r.status_code == 200


async def post_chat(
    client: httpx.AsyncClient,
    query: str,
    session_id: str,
) -> dict:
    """POST /chat 并解析 SSE 流，返首条 meta 事件"""
    r = await client.post(
        f"{BASE}/api/chat",
        json={"query": query, "session_id": session_id},
        headers={"Accept": "text/event-stream"},
        timeout=60.0,
    )
    if r.status_code != 200:
        return {"http_status": r.status_code, "error": r.text[:200]}

    events = []
    for line in r.text.split("\n"):
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return {"http_status": 200, "events": events}


async def get_metrics(client: httpx.AsyncClient) -> Optional[dict]:
    """GET /metrics 拿 JSON snapshot"""
    r = await client.get(f"{BASE}/api/metrics", timeout=10.0)
    if r.status_code != 200:
        return None
    return r.json()


def get_rewrite_count(snapshot: dict, key: str) -> int:
    """从 metrics snapshot 取 rewrite.by_reason[key]，缺省返 0"""
    if not snapshot:
        return 0
    rewrite = snapshot.get("rewrite") or {}
    by_reason = rewrite.get("by_reason") or {}
    return by_reason.get(key, 0)


async def wait_for_service(client: httpx.AsyncClient, max_wait: int = 30) -> bool:
    """等服务 ready（health 端点 200）"""
    for i in range(max_wait):
        try:
            r = await client.get(f"{BASE}/health", timeout=2.0)
            if r.status_code == 200:
                print(f"  服务就绪（{i+1}s）")
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def main() -> int:
    print("=" * 60)
    print("M12 query_rewriter e2e 验证")
    print("=" * 60)
    results: list[bool] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. 等服务
        print("\n[Setup] 等待服务就绪...")
        if not await wait_for_service(client):
            print(f"  {FAIL} 服务未启动，放弃")
            print(f"  启动方式: cd deploy && docker compose --env-file ../.env.dev up -d")
            return 1
        results.append(_print_step("service ready", True))

        # 2. 登录
        print("\n[Setup] 登录 demotest")
        ok = await login(client)
        results.append(_print_step("login", ok))
        if not ok:
            return 1

        # 3. 拿 before snapshot
        before = await get_metrics(client)
        if not before:
            print(f"  {FAIL} /metrics 不可访问")
            return 1
        before_rewritten = get_rewrite_count(before, "rewritten")
        before_skipped = get_rewrite_count(before, "skipped_no_coref")
        before_total = (before.get("rewrite") or {}).get("total", 0)
        print(f"\n[Before] rewrite.total={before_total} "
              f"by_reason={dict((before.get('rewrite') or {}).get('by_reason') or {})}")

        # 3.5 清 rcache（让 Round 1 不会被 sem cache 命中）
        print("\n[Setup] 清 rcache for user_id=7")
        clear_rcache_for_user(USER_ID)

        # 4. 多轮对话
        session_id = f"test-rewriter-{int(time.time())}"
        # unique tag 防止 response_cache exact 命中（Round 2 "它能便宜点吗" 之前的 e2e 跑过）
        unique_tag = int(time.time()) % 100000

        # Round 1: 无指代词 → 期望 skipped_no_coref +1
        # 注：query 要能过 guard L2（电商域 cosine > 0.55）
        print(f"\n[Round 1] 无指代词（电商域 query）")
        round1_query = f"iPhone 15 Pro 商品保修期多久问题{unique_tag}"
        print(f"  query: '{round1_query}'")
        resp1 = await post_chat(client, round1_query, session_id)
        if "error" in resp1:
            results.append(_print_step("Round 1 http", False, resp1["error"]))
        else:
            events = resp1["events"]
            meta = next((e for e in events if e.get("type") == "meta"), None)
            done = next((e for e in events if e.get("type") == "done"), None)
            ok1 = meta is not None and done is not None
            results.append(_print_step("Round 1 SSE", ok1, f"meta={meta is not None} done={done is not None}"))

        # 等一下让 metrics 写入
        await asyncio.sleep(0.5)

        # Round 2: 含指代 + 有 history → 期望 rewritten +1
        # unique tag 防 rcache 命中（之前的 e2e 跑过 "它能便宜点吗"）
        print(f"\n[Round 2] 含指代 + 有 history")
        round2_query = f"它能便宜点吗{unique_tag}"
        print(f"  query: '{round2_query}'")
        resp2 = await post_chat(client, round2_query, session_id)
        if "error" in resp2:
            results.append(_print_step("Round 2 http", False, resp2["error"]))
        else:
            events = resp2["events"]
            meta = next((e for e in events if e.get("type") == "meta"), None)
            done = next((e for e in events if e.get("type") == "done"), None)
            ok1 = meta is not None and done is not None
            results.append(_print_step("Round 2 SSE", ok1, f"meta={meta is not None} done={done is not None}"))

        # 5. 拿 after snapshot
        await asyncio.sleep(0.5)
        after = await get_metrics(client)
        if not after:
            print(f"  {FAIL} after /metrics 不可访问")
            return 1
        after_rewritten = get_rewrite_count(after, "rewritten")
        after_skipped = get_rewrite_count(after, "skipped_no_coref")
        after_total = (after.get("rewrite") or {}).get("total", 0)
        print(f"\n[After]  rewrite.total={after_total} "
              f"by_reason={dict((after.get('rewrite') or {}).get('by_reason') or {})}")

        # 6. 验证增量
        print(f"\n[Assert] by_reason 增量")
        results.append(_print_step(
            "rewritten +1",
            after_rewritten - before_rewritten >= 1,
            f"{before_rewritten} -> {after_rewritten} (Δ={after_rewritten - before_rewritten})"
        ))
        results.append(_print_step(
            "skipped_no_coref +1",
            after_skipped - before_skipped >= 1,
            f"{before_skipped} -> {after_skipped} (Δ={after_skipped - before_skipped})"
        ))
        results.append(_print_step(
            "rewrite.total +2",
            after_total - before_total >= 2,
            f"{before_total} -> {after_total} (Δ={after_total - before_total})"
        ))

    # 汇总
    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"SUMMARY: {passed}/{total} PASS")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))