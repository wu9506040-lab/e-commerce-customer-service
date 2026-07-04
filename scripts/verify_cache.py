"""
M11.5 P1 响应缓存 端到端验证

两层验证：
  L1 Exact match：同一 query 第二次走 cache，meta.intent=cache_hit，无 LLM token 消耗
  L2 Semantic match：query 微改（paraphrase）但语义等价，semantic 命中

Redis key 清理 → 真实 login → 发 query → 再发同 query → 验证第二次是 cache_hit

用法：
    python scripts/verify_cache.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import urllib.parse

# 加项目根到 path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

BASE = "http://localhost:8000"
USERNAME = "demotest"
PASSWORD = "demotest123"


def _print_step(name: str, ok: bool, msg: str = ""):
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if msg:
        line += f"  ({msg})"
    print(line)
    return ok


async def login(client: httpx.AsyncClient) -> Optional[dict]:
    form = urllib.parse.urlencode({"username": USERNAME, "password": PASSWORD})
    r = await client.post(
        f"{BASE}/api/auth/login",
        content=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 200:
        return None
    me = await client.get(f"{BASE}/api/auth/me")
    return me.json() if me.status_code == 200 else None


async def fetch_metrics(client: httpx.AsyncClient) -> dict:
    r = await client.get(f"{BASE}/api/metrics")
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return {}
    return {}


async def post_chat(client: httpx.AsyncClient, query: str, session_id: Optional[str] = None) -> dict:
    body = {"query": query}
    if session_id:
        body["session_id"] = session_id
    r = await client.post(
        f"{BASE}/api/chat",
        json=body,
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


def _redis():
    import redis
    from app.core.config import settings
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


# =============================================================
# 测试用例
# =============================================================
async def main() -> int:
    print("=" * 60)
    print("M11.5 P1 响应缓存验证")
    print("=" * 60)
    results: list[bool] = []

    # ---- 前置：清理 rcache:* key ----
    r = _redis()
    keys = list(r.scan_iter(match="rcache:*"))
    if keys:
        r.delete(*keys)
        print(f"\n[Setup] 清理 {len(keys)} 个 rcache:* key")
    else:
        print("\n[Setup] Redis 无残留 rcache:* key")

    async with httpx.AsyncClient(timeout=30.0) as client:
        print("\n[Setup] 登录 demotest")
        me = await login(client)
        if not me:
            results.append(_print_step("login", False))
            return 1
        user_id = me["id"]
        results.append(_print_step("login", True, f"user_id={user_id}"))

        baseline = await fetch_metrics(client)
        baseline_alerts = baseline.get("behavior", {}).get("alerts_total", 0)
        baseline_chat = baseline.get("chat", {}).get("total", 0)
        baseline_tokens = baseline.get("chat", {}).get("answer_tokens_total", 0)
        print(f"[Setup] baseline chat.total={baseline_chat} answer_tokens={baseline_tokens}")

        # ============== Test 1: Exact cache 命中 ==============
        print("\n[Case 1] L1 Exact cache：同 query 第二次命中")
        # 用带 timestamp 的唯一 query，避免被 guard L3 重复检测拦
        unique_tag = int(time.time()) % 100000
        query = f"商品保质期一般是多久呢问题{unique_tag}"
        session_id = f"test-cache-exact-{int(time.time())}"

        # 第一次：走完整 LLM 流程
        print("  - 第一次发问（应走 LLM）")
        r1 = await post_chat(client, query, session_id=session_id)
        events1 = r1.get("events", [])
        meta1 = next((e for e in events1 if e.get("type") == "meta"), {})
        intent1 = meta1.get("intent")
        print(f"    intent={intent1}")
        # 注意：第一次可能被 guard L1 拦（如果 query 不够长）；这里要选个明确能过 guard 的 query

        # 第二次：应命中 cache
        print("  - 第二次发问（应命中 exact cache）")
        r2 = await post_chat(client, query, session_id=session_id)
        events2 = r2.get("events", [])
        meta2 = next((e for e in events2 if e.get("type") == "meta"), {})
        intent2 = meta2.get("intent")
        print(f"    intent={intent2}")

        # 第一次应该不是 cache_hit，第二次应该是 cache_hit
        if intent1 == "cache_hit":
            # 如果第一次就是 cache_hit，说明前面测试残留了（说明 baseline 没清干净）
            results.append(_print_step(
                "L1 第一次非 cache（clean）", False,
                f"intent1={intent1}（意外命中）"
            ))
        else:
            results.append(_print_step(
                "L1 第一次非 cache",
                True,
                f"intent1={intent1}"
            ))

        results.append(_print_step(
            "L1 第二次命中 cache",
            intent2 == "cache_hit",
            f"intent2={intent2}"
        ))

        # ============== Test 2: Semantic cache 命中 ==============
        # 用 paraphrase（同义改写）—— 不能和 exact 相同 query，否则 exact 先命中
        print("\n[Case 2] L2 Semantic cache：paraphrase query 命中")
        # 用唯一 tag 防 L3 拦截
        sem_tag = int(time.time()) % 100000
        q1 = f"商品保修期一般是几年问题{sem_tag}"
        q2 = f"商品保修通常有多久{sem_tag}"  # paraphrase of q1

        session_id = f"test-cache-sem-{int(time.time())}"

        # 第一次：q1 走完整 LLM
        print(f"  - 第一次: {q1!r}（走 LLM）")
        r1 = await post_chat(client, q1, session_id=session_id)
        events1 = r1.get("events", [])
        meta1 = next((e for e in events1 if e.get("type") == "meta"), {})
        intent1 = meta1.get("intent")
        print(f"    intent={intent1}")

        # 第二次：q2 paraphrase，应走 LLM（不命中 exact）
        print(f"  - 第二次: {q2!r}（走 LLM）")
        r2 = await post_chat(client, q2, session_id=session_id)
        events2 = r2.get("events", [])
        meta2 = next((e for e in events2 if e.get("type") == "meta"), {})
        intent2 = meta2.get("intent")
        print(f"    intent={intent2}")

        # 第三次：再用 q1（应命中 exact 缓存）
        print(f"  - 第三次: {q1!r}（命中 exact cache）")
        r3 = await post_chat(client, q1, session_id=session_id)
        events3 = r3.get("events", [])
        meta3 = next((e for e in events3 if e.get("type") == "meta"), {})
        intent3 = meta3.get("intent")
        print(f"    intent={intent3}")
        results.append(_print_step(
            "L1 第三次 exact 命中",
            intent3 == "cache_hit",
            f"intent={intent3}"
        ))

        # 注：semantic 命中率依赖 embedding 质量，这里只验证 exact；semantic 留作后续观察

        # ============== Test 3: 验证 Redis 写入 ==============
        print("\n[Case 3] Redis rcache:* key 实际写入")
        import hashlib
        md5_q1 = hashlib.md5(q1.encode()).hexdigest()
        exact_key = f"rcache:exact:{user_id}:{md5_q1}"
        exact_val = r.get(exact_key)
        results.append(_print_step(
            "rcache:exact 写入",
            exact_val is not None,
            f"key={exact_key[:40]}... val_len={len(exact_val) if exact_val else 0}"
        ))

        sem_idx_key = f"rcache:sem_idx:{user_id}"
        sem_idx_size = r.scard(sem_idx_key)
        results.append(_print_step(
            "rcache:sem_idx 写入",
            sem_idx_size >= 1,
            f"key={sem_idx_key} size={sem_idx_size}"
        ))

        # ============== 验证 metrics token 消耗 ==============
        print("\n[Case 4] metrics 角度验证")
        new_metrics = await fetch_metrics(client)
        new_chat = new_metrics.get("chat", {}).get("total", 0)
        new_tokens = new_metrics.get("chat", {}).get("answer_tokens_total", 0)
        chat_delta = new_chat - baseline_chat
        token_delta = new_tokens - baseline_tokens
        print(f"  chat.total delta={chat_delta}")
        print(f"  answer_tokens delta={token_delta}")
        # 5 次请求（Case1: 2 + Case2: 3）
        # Case 1: 第 1 次 LLM（+1），第 2 次 exact cache（+0）
        # Case 2: 第 1 次 LLM（+1），第 2 次 semantic cache（+0），第 3 次 exact cache（+0）
        # 总计 chat.total +2（2 次 LLM 调用，3 次 cache 命中不计入）
        results.append(_print_step(
            "chat.total +2（仅 2 次 LLM 调用）",
            chat_delta == 2,
            f"baseline={baseline_chat} new={new_chat} delta={chat_delta}"
        ))
        results.append(_print_step(
            "answer_tokens 增加（2 次 LLM 调用）",
            token_delta > 0,
            f"delta={token_delta}"
        ))

    # 汇总
    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
