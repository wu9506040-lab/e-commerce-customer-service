"""
Sprint P2 / SSE Resume: 后端 checkpoint + resume 端点单元验证
不依赖浏览器，跑通即证明 backend resume 链路没问题

流程：
1. POST /chat → 收两个 token → 断开（abort）
2. POST /chat/resume → 拿到 prefix_text + done
"""
import json
import socket
import urllib.request
import urllib.parse
import urllib.error
import sys
import threading
import time


BASE = "http://localhost:8000"


def http_post_json(path: str, body: dict, cookie: str | None = None) -> tuple[int, dict | str, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Cookie": cookie} if cookie else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            headers = dict(resp.headers.items())
            try:
                return resp.status, json.loads(raw), headers
            except Exception:
                return resp.status, raw, headers
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8")), dict(e.headers.items())
        except Exception:
            return e.code, str(e), dict(e.headers.items())


def get_cookies(resp_headers: dict) -> str:
    setc = resp_headers.get("Set-Cookie") or resp_headers.get("set-cookie") or ""
    if not setc:
        return ""
    # take first cookie
    return setc.split(";")[0]


def main() -> int:
    # 1. 登录
    print("[1/4] login...")
    pw = "resume_test_123"
    username = "resume_test"
    # 先尝试注册（已存在则忽略 400）
    http_post_json(
        "/api/auth/register",
        {"username": username, "password": pw, "email": "rt@test.com"},
    )
    # 登录
    form = urllib.parse.urlencode({"username": username, "password": pw}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/auth/login",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        cookie = get_cookies(dict(resp.headers.items()))
        print(f"      cookie: {cookie[:50]}...")

    # 2. POST /chat 流式收集前 2 个 token
    print("\n[2/4] POST /chat collect first 2 tokens + abort...")
    sid = "curl-test-" + str(int(time.time() * 1000))
    query = "退款流程是怎样的？"

    # 手动打开流，读取部分后 abort
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=json.dumps({"query": query, "session_id": sid, "sku": None, "order_no": None}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cookie": cookie,
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print(f"      FAIL: {e}")
        return 1

    stream_id: str | None = None
    last_event_id: int | None = None
    events: list[dict] = []

    try:
        buf = ""
        while True:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            # 每遇到 \n\n 切一段
            while "\n\n" in buf:
                part, buf = buf.split("\n\n", 1)
                # 解析 id + data
                current_id: int | None = None
                data_str: str | None = None
                for line in part.split("\n"):
                    line = line.strip()
                    if line.startswith("id:"):
                        try:
                            current_id = int(line[3:].strip())
                        except ValueError:
                            pass
                    elif line.startswith("data:"):
                        data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    ev = json.loads(data_str)
                except Exception:
                    continue
                if current_id is not None:
                    ev["id"] = current_id
                events.append(ev)
                # 收到 meta 拿到 stream_id
                if ev.get("type") == "meta":
                    stream_id = ev.get("stream_id")
                # 拿到 2 个 token 后停
                tokens = [e for e in events if e.get("type") == "token"]
                if len(tokens) >= 2 and stream_id:
                    print(f"      collected {len(tokens)} tokens, stream_id={stream_id}")
                    last_event_id = ev["id"]
                    # 关 socket 模拟断开
                    try:
                        # 用底层 close 中断连接
                        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)  # type: ignore
                    except Exception:
                        try:
                            resp.close()
                        except Exception:
                            pass
                    break
    except Exception as e:
        print(f"      read interrupted: {e}")

    if not stream_id:
        print(f"      FAIL: no stream_id. events={events[:3]}")
        return 1
    print(f"      last_event_id={last_event_id}")

    # 3. POST /chat/resume
    print(f"\n[3/4] POST /chat/resume (stream_id={stream_id})...")
    req = urllib.request.Request(
        f"{BASE}/api/chat/resume",
        data=json.dumps({
            "session_id": sid,
            "stream_id": stream_id,
            "query": query,
            "last_event_id": last_event_id,
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cookie": cookie,
        },
        method="POST",
    )

    resume_events: list[dict] = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            buf = ""
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    part, buf = buf.split("\n\n", 1)
                    current_id = None
                    data_str = None
                    for line in part.split("\n"):
                        line = line.strip()
                        if line.startswith("id:"):
                            try:
                                current_id = int(line[3:].strip())
                            except ValueError:
                                pass
                        elif line.startswith("data:"):
                            data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        ev = json.loads(data_str)
                        if current_id is not None:
                            ev["id"] = current_id
                        resume_events.append(ev)
                    except Exception:
                        pass
    except urllib.error.HTTPError as e:
        print(f"      FAIL: status={e.code}, body={e.read().decode('utf-8', errors='replace')[:200]}")
        return 1

    print(f"      received {len(resume_events)} resume events:")
    for ev in resume_events:
        t = ev.get("type", "?")
        if t == "resume_prefix":
            prefix = ev.get("prefix_text", "")[:60]
            print(f"        [id={ev.get('id')}] {t}: {prefix!r}")
        else:
            print(f"        [id={ev.get('id')}] {t}: {str(ev)[:80]}")

    # 4. 判定
    print("\n[4/4] verdict:")
    has_prefix = any(e.get("type") == "resume_prefix" for e in resume_events)
    has_done = any(e.get("type") == "done" for e in resume_events)
    has_closed = any(e.get("type") == "closed" for e in resume_events)
    print(f"      resume_prefix: {has_prefix}")
    print(f"      done: {has_done}")
    print(f"      closed: {has_closed}")

    ok = has_prefix and has_done and has_closed
    print(f"\nFinal: {'PASS' if ok else 'FAIL'} (need resume_prefix+done+closed)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
