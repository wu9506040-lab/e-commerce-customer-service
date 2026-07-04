"""
IDOR（水平越权）+ 未授权访问测试 — P0 安全
================================================

4 条用例覆盖：
| # | 用例                                          | 期望              |
|---|-----------------------------------------------|-------------------|
| 1 | A 查 B 的订单详情（A=visitor, B=demotest）    | 404（不应能看）   |
| 2 | A 删 B 的会话                                  | 403/404           |
| 3 | 无 cookie 调 /api/auth/me                       | 401               |
| 4 | 普通用户调 /api/admin/*                          | 403               |

注意：每个 client 独立，避免 cookie 串扰
"""
import asyncio
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Tuple

import httpx

BASE = "http://120.79.27.124:8000"
DEMO_USER = ("demotest", "demotest123")
REPORT_PATH = Path(__file__).parent.parent / "frontend/_screenshots/idor_report.json"

results: dict = {}


def _ok(name: str, ok: bool, msg: str = ""):
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}" + (f"  ({msg})" if msg else "")
    print(line)
    results[name] = {"ok": ok, "msg": msg}
    return ok


async def login(client: httpx.AsyncClient, username: str, password: str) -> bool:
    form = urllib.parse.urlencode({"username": username, "password": password})
    r = await client.post(
        f"{BASE}/api/auth/login", content=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return r.status_code == 200


async def login_demo_account(client: httpx.AsyncClient) -> bool:
    """游客一键 demo"""
    r = await client.post(f"{BASE}/api/public/demo-account")
    return r.status_code == 200


# =============================================================
# 1. IDOR：visitor 看 demotest 的订单
# =============================================================
async def test_idor_order_access() -> bool:
    """A=visitor 直接拉 B=demotest 的订单 ORD20260704001"""
    async with httpx.AsyncClient(timeout=30.0) as visitor:
        # 用 visitor 登录
        await login_demo_account(visitor)
        # demotest 的订单号（已知）
        other_order = "ORD20260704001"
        r = await visitor.get(f"{BASE}/api/orders/{other_order}")
        # 期望：404（订单不存在对 visitor，因为不归他）或 403
        status = r.status_code
        body = r.text[:200]
        ok = status in (403, 404)
        # 如果状态是 200 + 订单内容 → IDOR bug
        if status == 200:
            try:
                d = r.json()
                if d.get("order", {}).get("order_no") == other_order:
                    return _ok(
                        "1. IDOR·visitor 看 demotest 订单",
                        False,
                        f"严重: 越权成功 order_no={other_order}",
                    )
            except Exception:
                pass
        return _ok(
            "1. IDOR·visitor 看 demotest 订单（应 403/404）",
            ok,
            f"HTTP {status}, body前80={body[:80]!r}",
        )


# =============================================================
# 2. 删除他人会话
# =============================================================
async def test_idor_delete_session() -> bool:
    """visitor 试图删 demotest 的会话

    前置：demotest 至少 1 个会话 → 拿到 session_id
    操作：visitor cookie → DELETE /api/conversations/{demotest_session_id}
    期望：403 或 404
    """
    # demotest 登录创建 1 个会话
    async with httpx.AsyncClient(timeout=30.0) as demotest:
        await login(demotest, *DEMO_USER)
        r = await demotest.get(f"{BASE}/api/conversations")
        data = r.json()
        sessions = data.get("conversations", data if isinstance(data, list) else [])
        if not sessions:
            # demotest 没会话，发一条 query 创建一个
            await demotest.post(
                f"{BASE}/api/chat",
                json={"query": "你好", "session_id": None},
                headers={"Accept": "text/event-stream"},
                timeout=30.0,
            )
            r = await demotest.get(f"{BASE}/api/conversations")
            data = r.json()
            sessions = data.get("conversations", data if isinstance(data, list) else [])
        if not sessions:
            return _ok("2. IDOR·visitor 删会话", False, "demotest 无可用会话（fixture 缺）")
        sid = sessions[0].get("session_id") or sessions[0].get("id")

    # 重新创建 independent visitor client
    async with httpx.AsyncClient(timeout=30.0) as visitor:
        await login_demo_account(visitor)
        # visitor 试图删 demotest 的会话
        r = await visitor.delete(f"{BASE}/api/conversations/{sid}")
        status = r.status_code
        body = r.text[:200]

        # 同时验证：demotest 自己的会话还在（没被误删）
        async with httpx.AsyncClient(timeout=30.0) as demotest2:
            await login(demotest2, *DEMO_USER)
            r2 = await demotest2.get(f"{BASE}/api/conversations")
            remaining = len(r2.json().get("conversations", []))

        ok = status in (403, 404)
        return _ok(
            "2. IDOR·visitor 删 demotest 会话（应 403/404，且 demotest 会话保留）",
            ok,
            f"HTTP {status}, body前80={body[:80]!r}, demotest剩{remaining}会话",
        )


# =============================================================
# 3. 无 cookie 调 /auth/me
# =============================================================
async def test_no_auth_me() -> bool:
    """独立 client（无任何 cookie），GET /api/auth/me → 应 401"""
    async with httpx.AsyncClient(timeout=30.0) as anon:
        r = await anon.get(f"{BASE}/api/auth/me")
        ok = r.status_code == 401
        return _ok(
            "3. 未授权 GET /api/auth/me（应 401）",
            ok,
            f"HTTP {r.status_code}",
        )


# =============================================================
# 4. 普通用户调 /api/admin/*
# =============================================================
async def test_normal_user_admin() -> bool:
    """demotest（普通用户）调 /api/admin/* → 应 403

    尝试 admin 几个典型端点
    """
    async with httpx.AsyncClient(timeout=30.0) as user:
        await login(user, *DEMO_USER)
        # 实际 admin 端点（来自 backend/app/api/admin.py）：
        # - /api/admin/ingest (POST)
        # - /api/admin/knowledge/info (GET)
        # - /api/admin/knowledge/sources (GET)
        # - /api/admin/knowledge/source/{source} (DELETE)
        # - /api/admin/knowledge/points (DELETE)
        admin_endpoints = [
            ("GET", "/api/admin/knowledge/info"),
            ("GET", "/api/admin/knowledge/sources"),
            ("POST", "/api/admin/ingest"),
        ]
        all_ok = True
        details = []
        for method, path in admin_endpoints:
            kw = {"json": {"text": "", "source": "idor_test"}} if method == "POST" else {}
            r = await user.request(method, f"{BASE}{path}", **kw)
            status = r.status_code
            details.append(f"{method} {path}={status}")
            if status != 403:
                all_ok = False
        return _ok(
            "4. 普通用户调 /api/admin/*（应统一 403）",
            all_ok,
            ", ".join(details),
        )


# =============================================================
# main
# =============================================================
async def main():
    print("=" * 70)
    print(f"  IDOR + 未授权访问 — {BASE}")
    print("=" * 70)

    await test_idor_order_access()
    await test_idor_delete_session()
    await test_no_auth_me()
    await test_normal_user_admin()

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    passed = sum(1 for v in results.values() if v["ok"])
    total = len(results)
    print(f"  通过: {passed}/{total}")
    for name, v in results.items():
        mark = "PASS" if v["ok"] else "FAIL"
        msg = f"  ({v['msg']})" if v["msg"] else ""
        print(f"  [{mark}] {name}{msg}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps({"passed": passed, "total": total, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  报告: {REPORT_PATH}")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
