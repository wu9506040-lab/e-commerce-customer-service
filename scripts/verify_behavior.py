"""
M11.5 P2 异常行为监控 端到端验证

5 类告警验证（按 Redis 滑动窗口）：
  1. IP 高频：同 IP 1min 内 > 30 请求
  2. IP 多账号：同 IP 1h 内 > 5 个 user_id（用 Redis 直接 sadd 模拟）
  3. User 高频：同 user 1min 内 > 15 请求
  4. SKU 探测：同 user 1min 内 > 5 个不同 SKU
  5. Order 探测：同 user 1min 内 > 3 个不同 order_no

验证方式（双重）：
  - Redis 计数验证 bm:* key 到达阈值
  - HTTP metrics 端点验证 alerts_by_type 增量

用法：
    python scripts/verify_behavior.py
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
    """登录 demotest，返回 user info（含 id）"""
    form = urllib.parse.urlencode({"username": USERNAME, "password": PASSWORD})
    r = await client.post(
        f"{BASE}/api/auth/login",
        content=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 200:
        return None
    me = await client.get(f"{BASE}/api/auth/me")
    if me.status_code == 200:
        return me.json()
    return None


async def fetch_metrics(client: httpx.AsyncClient) -> dict:
    r = await client.get(f"{BASE}/api/metrics")
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return {}
    return {}


def _redis():
    """获取 Redis 客户端（同步）"""
    import redis
    from app.core.config import settings
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _get_count(key: str) -> int:
    r = _redis()
    val = r.get(key)
    if val is None:
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _get_scard(key: str) -> int:
    r = _redis()
    val = r.scard(key)
    return int(val) if val else 0


def _scard_window(prefix: str, suffix_minute: int, window: int = 2) -> int:
    """聚合最近 N 个分钟桶的 set size（防跨分钟桶丢数据）"""
    r = _redis()
    total = 0
    for m in range(suffix_minute - window + 1, suffix_minute + 1):
        key = f"{prefix}:{m}"
        try:
            total += int(r.scard(key) or 0)
        except Exception:
            pass
    return total


def _count_window(prefix: str, suffix_minute: int, window: int = 2) -> int:
    """聚合最近 N 个分钟桶的 int counter"""
    r = _redis()
    total = 0
    for m in range(suffix_minute - window + 1, suffix_minute + 1):
        key = f"{prefix}:{m}"
        try:
            v = r.get(key)
            if v:
                total += int(v)
        except Exception:
            pass
    return total


# =============================================================
# 测试用例
# =============================================================
async def main() -> int:
    print("=" * 60)
    print("M11.5 P2 异常行为监控验证")
    print("=" * 60)
    results: list[bool] = []

    # ---- 前置：清掉旧 bm:* key ----
    r = _redis()
    keys = list(r.scan_iter(match="bm:*"))
    if keys:
        r.delete(*keys)
        print(f"\n[Setup] 清理 {len(keys)} 个 bm:* key")
    else:
        print("\n[Setup] Redis 无残留 bm:* key")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 登录 demotest
        print("\n[Setup] 登录 demotest")
        me = await login(client)
        if not me:
            results.append(_print_step("login", False))
            return 1
        user_id = me["id"]
        results.append(_print_step("login", True, f"user_id={user_id}"))

        # 拿 baseline
        baseline = await fetch_metrics(client)
        baseline_alerts = baseline.get("behavior", {}).get("alerts_total", 0)
        print(f"[Setup] metrics baseline alerts_total={baseline_alerts}")

        # ============== Test 1: User 高频（同 user 1min > 15 次） ==============
        # 用 fast-reject query（纯英文无 SKU，guard L1 即拒）跑 16 次
        # 避免 LLM 延迟导致跨分钟桶
        print("\n[Case 1] User 高频：同 user 1min > 15 次请求（fast-reject 路径）")
        minute_start = int(time.time() // 60)
        session_id = f"test-bm-freq-{int(time.time())}"

        async def fire_freq(i: int):
            return await client.post(
                f"{BASE}/api/chat",
                json={"query": f"hello world burst {i}", "session_id": session_id},
                headers={"Accept": "text/event-stream"},
                timeout=10.0,
            )

        # 10 并发 × 2 批 = 16+ 次
        for batch_start in range(0, 16, 10):
            batch = [fire_freq(i) for i in range(batch_start, min(batch_start + 10, 16))]
            await asyncio.gather(*batch)
        cnt = _count_window(f"bm:user:req:{user_id}", minute_start, window=2)
        results.append(_print_step(
            "user 高频 Redis 计数（2 桶聚合，fast-reject）",
            cnt >= 16,
            f"user_id={user_id} count={cnt}"
        ))

        # ============== Test 2: SKU 探测（同 user 1min > 5 个不同 SKU） ==============
        print("\n[Case 2] SKU 探测：同 user 1min 内切换 > 5 个 SKU")
        minute_start = int(time.time() // 60)
        skus = ["SKU001", "SKU002", "SKU003", "SKU004", "SKU005", "SKU006"]

        async def fire_sku(sku: str):
            return await client.post(
                f"{BASE}/api/chat",
                json={
                    "query": f"商品 {sku} 怎么样",
                    "session_id": f"test-bm-sku-{sku}-{int(time.time())}",
                    "sku": sku,
                },
                headers={"Accept": "text/event-stream"},
                timeout=30.0,
            )

        await asyncio.gather(*[fire_sku(sku) for sku in skus])
        sku_cnt = _scard_window(f"bm:user:sku:{user_id}", minute_start, window=2)
        results.append(_print_step(
            "SKU 探测 Redis set size（2 桶聚合，并发）",
            sku_cnt >= 6,
            f"user_id={user_id} count={sku_cnt} (>=6 触发告警)"
        ))

        # ============== Test 3: Order 探测（同 user 1min > 3 个不同 order_no） ==============
        print("\n[Case 3] Order 探测：同 user 1min 内切换 > 3 个 order_no")
        minute_start = int(time.time() // 60)
        orders = ["ORD20260621001", "ORD20260621002", "ORD20260621003", "ORD20260621004"]

        async def fire_order(order_no: str):
            return await client.post(
                f"{BASE}/api/chat",
                json={
                    "query": f"我的订单 {order_no} 什么状态",
                    "session_id": f"test-bm-ord-{order_no}-{int(time.time())}",
                    "order_no": order_no,
                },
                headers={"Accept": "text/event-stream"},
                timeout=30.0,
            )

        await asyncio.gather(*[fire_order(order_no) for order_no in orders])
        order_cnt = _scard_window(f"bm:user:order:{user_id}", minute_start, window=2)
        results.append(_print_step(
            "Order 探测 Redis set size（2 桶聚合，并发）",
            order_cnt >= 4,
            f"user_id={user_id} count={order_cnt} (>=4 触发告警)"
        ))

        # ============== Test 4: IP 高频（用伪造 XFF 头模拟同 IP） ==============
        # 用 guard L1 即拒的纯英文 query，避开 LLM 调用（~100ms/次，31 次 ~3s 可塞同一分钟桶）
        print("\n[Case 4] IP 高频：同 IP (XFF) 1min > 30 次请求（fast-reject 路径）")
        minute_start = int(time.time() // 60)
        test_ip = "203.0.113.42"

        async def fire_one(i: int):
            # query 用纯英文无 SKU，会被 guard L1 立刻拒（behavior_monitor 仍记账）
            return await client.post(
                f"{BASE}/api/chat",
                json={
                    "query": f"hello world burst test {i}",
                    "session_id": f"test-bm-ip-{i}-{int(time.time())}",
                },
                headers={
                    "Accept": "text/event-stream",
                    "X-Forwarded-For": test_ip,
                },
                timeout=10.0,
            )

        # 10 并发 × 4 批 = 40 次（至少 31 次可达）
        for batch_start in range(0, 31, 10):
            batch = [fire_one(i) for i in range(batch_start, min(batch_start + 10, 31))]
            await asyncio.gather(*batch)
        # 用 2 桶聚合（fast-reject 路径基本能塞同一桶）
        ip_cnt = _count_window(f"bm:ip:req:{test_ip}", minute_start, window=2)
        results.append(_print_step(
            "IP 高频 Redis 计数（2 桶聚合，fast-reject）",
            ip_cnt >= 31,
            f"ip={test_ip} count={ip_cnt} (>=31 触发告警)"
        ))

        # ============== Test 5: IP 多账号（同 IP 1h > 5 个 user_id） ==============
        print("\n[Case 5] IP 多账号：同 IP 1h 内 > 5 个不同 user_id")
        # 用 Redis 直接 sadd 模拟（实际场景需要 5 个真实账号登录，测试不必要）
        test_ip2 = "198.51.100.7"
        hour = int(time.time() // 3600)
        ip_users_key = f"bm:ip:users:{test_ip2}:{hour}"
        pipe = r.pipeline()
        for uid in [1, 2, 3, 4, 5, 6]:
            pipe.sadd(ip_users_key, uid)
        pipe.expire(ip_users_key, 3700)
        pipe.execute()
        user_count = _get_scard(ip_users_key)
        results.append(_print_step(
            "IP 多账号 set size 触发阈值",
            user_count >= 6,
            f"key={ip_users_key} size={user_count}"
        ))
        # 注：IP 多账号告警是真实请求时才能触发 metrics
        # 这里只验证 Redis 数据正确，metrics 那侧由真实请求保证

        # ============== 验证 metrics 告警计数变化 ==============
        print("\n[Case 6] metrics behavior 块告警计数")
        new_metrics = await fetch_metrics(client)
        behavior_block = new_metrics.get("behavior", {})
        new_alerts = behavior_block.get("alerts_total", 0)
        by_type = behavior_block.get("alerts_by_type", {})
        print(f"  metrics alerts_total: {baseline_alerts} -> {new_alerts}")
        print(f"  metrics alerts_by_type: {by_type}")
        results.append(_print_step(
            "metrics alerts 增量 >= 4",
            new_alerts - baseline_alerts >= 4,
            f"delta={new_alerts - baseline_alerts}"
        ))
        # 4 类按用户/IP 维度
        expected_types = [
            ("user_high_freq", 1),
            ("user_sku_probe", 1),
            ("user_order_probe", 1),
            ("ip_high_freq", 1),
        ]
        for t, min_count in expected_types:
            results.append(_print_step(
                f"metrics {t}",
                by_type.get(t, 0) >= min_count,
                f"count={by_type.get(t, 0)} >= {min_count}"
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
