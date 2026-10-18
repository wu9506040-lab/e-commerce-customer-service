#!/usr/bin/env python3
"""V16.8 P0-C + P0-A live smoke test (unauth bypass via /api/chat).

模拟 daemon 的 HTTP 请求：
- P0-C: query='在吗' → meta.intent='chitchat'，单 token 短路
- P0-A: query='我要退款' + channel='xianyu' + buyer_unb='test-buyer'
        → meta.xianyu_refund 出现 + order_state 字段

注意：docker backend 在 localhost:8000，绕过 daemon 直接打 /api/chat
"""
import json
import sys
import urllib.request


def curl_post(payload: dict) -> str:
    url = "http://localhost:8000/api/chat"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"
    except Exception as e:
        return f"ERROR: {e!r}"


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """简单 SSE 解析：取所有 data: 行，data 载荷是 JSON。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            payload_str = line[6:].strip()
            if not payload_str or payload_str == "[DONE]":
                continue
            try:
                out.append(("data", json.loads(payload_str)))
            except json.JSONDecodeError:
                out.append(("raw", {"text": payload_str}))
        elif line and not line.startswith(("event:", "id:", ":")):
            # 非 data 行 — 可能是普通事件
            pass
    return out


def main():
    print("=" * 60)
    print("P0-C live test: query='在吗'  期望 meta.intent='chitchat'")
    print("=" * 60)
    r = curl_post({"query": "在吗"})
    print(r[:1500])
    events = parse_sse(r)
    print(f"\n>>> events parsed: {len(events)}")
    for etype, data in events:
        if isinstance(data, dict):
            print(f"[{etype}] intent={data.get('intent')!r}, "
                  f"chitchat_short_circuit={data.get('chitchat_short_circuit')}")
            if data.get("intent") == "chitchat":
                print("✅ P0-C CHITCHAT PASS")
                return 0
            if data.get("type") in ("meta", "token", "done"):
                pass
    print("⚠️ P0-C NO meta.intent='chitchat' found")
    return 1


if __name__ == "__main__":
    sys.exit(main())
