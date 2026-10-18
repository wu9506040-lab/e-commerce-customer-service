#!/usr/bin/env python3
"""V16.8 P0-A + P0-C live smoke（统一脚本，避开 GBK print）"""
import json
import sys
import urllib.request


def curl_post(payload: dict, timeout: int = 30) -> str:
    url = "http://localhost:8000/api/chat"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"
    except Exception as e:
        return f"ERROR: {e!r}"


def parse_sse(text: str) -> list[tuple[str, dict]]:
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
    return out


def p0c_chitchat() -> int:
    print("=" * 60)
    print("P0-C: query='在吗' 期望 meta.intent='chitchat' + 1 token 短路")
    print("=" * 60)
    r = curl_post({"query": "在吗"})
    events = parse_sse(r)
    metas = [d for t, d in events if t == "data" and isinstance(d, dict) and d.get("type") == "meta"]
    metas2 = [d for t, d in events if t == "data" and isinstance(d, dict) and "intent" in d]
    tokens = [d for t, d in events if t == "data" and isinstance(d, dict) and d.get("type") == "token"]
    dones = [d for t, d in events if t == "data" and isinstance(d, dict) and d.get("type") == "done"]

    meta = metas2[0] if metas2 else {}
    print(f"meta.intent={meta.get('intent')!r}")
    print(f"meta.chitchat_short_circuit={meta.get('chitchat_short_circuit')!r}")
    print(f"intent_method={meta.get('intent_method')!r}, intent_confidence={meta.get('intent_confidence')!r}")
    print(f"tokens={len(tokens)} (期望 1)")
    if tokens:
        # token 文本里可能有'在的'或'?'等关键词（中文编码在某些 log 里会乱码）
        token_text = tokens[0].get("text", "")
        print(f"first token text len={len(token_text)}, starts_with={token_text[:6]!r}")
    print(f"done events={len(dones)}")

    # 判据
    assert meta.get("intent") == "chitchat", f"期望 chitchat，实际 {meta.get('intent')}"
    assert meta.get("chitchat_short_circuit") is True, "chitchat_short_circuit 应为 True"
    assert len(tokens) == 1, f"chitchat 短路应是 1 token，实际 {len(tokens)}"
    print("\n[P0-C PASS] chitchat 短路生效")
    return 0


def p0a_refund_channel() -> int:
    print("\n" + "=" * 60)
    print("P0-A: query='我要退款' + channel='xianyu' + buyer_unb='live-test-buyer'")
    print("    期望 meta.xianyu_refund 出现 + 不调商城 OrderService")
    print("=" * 60)
    r = curl_post({
        "query": "我要退款",
        "channel": "xianyu",
        "buyer_unb": "live-test-buyer",
    })
    events = parse_sse(r)
    metas = [d for t, d in events if t == "data" and isinstance(d, dict) and "intent" in d]
    tokens = [d for t, d in events if t == "data" and isinstance(d, dict) and d.get("type") == "token"]
    dones = [d for t, d in events if t == "data" and isinstance(d, dict) and d.get("type") == "done"]

    if not metas:
        print("ERROR: no meta events")
        print(r[:1500])
        return 1
    meta = metas[0]
    print(f"meta.intent={meta.get('intent')!r}")
    print(f"meta.channel={meta.get('channel')!r}")
    xr = meta.get("xianyu_refund") or {}
    print(f"meta.xianyu_refund.order_state={xr.get('order_state')!r}")
    print(f"meta.xianyu_refund.should_reject={xr.get('should_reject')!r}")
    print(f"meta.xianyu_refund.should_escalate={xr.get('should_escalate')!r}")
    print(f"meta.xianyu_refund.reject_reason={xr.get('reject_reason')!r}")
    combined = "".join(t.get("text", "") for t in tokens)
    print(f"answer (combined tokens) len={len(combined)}")
    print(f"answer preview: {combined[:120]!r}")

    # 判据
    assert meta.get("intent") == "refund_query"
    assert meta.get("channel") == "xianyu"
    assert xr, "meta 必含 xianyu_refund 元数据（P0-A 路由生效证据）"
    # bridge 路径 → 不是 'ask_login' / '用户尚未登录' / '登录拦截' 关键词
    bad_keywords = ["用户尚未登录", "登录拦截", "未登录", "请先登录", "Admin Login Required"]
    has_bad = any(kw in combined for kw in bad_keywords)
    assert not has_bad, f"出现商城登录拦截关键词，bridge 路由可能被绕过: combined={combined!r}"
    # order_state 必填（unknown 或 refunded/paid）
    assert xr.get("order_state") in ("unknown", "refunded", "paid", "shipped", "signed", "received"), \
        f"order_state 异常: {xr.get('order_state')!r}"
    # 无 kwargs context（无 kb_hits/products_found → 不走 RAG）
    assert meta.get("kb_hits", 0) == 0, f"P0-A 不应触发 RAG 检索，实际 kb_hits={meta.get('kb_hits')}"
    print("\n[P0-A PASS] xianyu_refund 元数据出现 + 未触发商城登录拦截 + 未触发 RAG")
    return 0


def regression_web_channel() -> int:
    """回归：商城 channel（默认 None）走商城路径，refund 不应含 xianyu_refund 字段"""
    print("\n" + "=" * 60)
    print("REGRESSION: channel=None refund 走商城 V3/V2，不应含 xianyu_refund")
    print("=" * 60)
    r = curl_post({
        "query": "我要退款",
        # channel 不传 = 商城（向后兼容）
    })
    events = parse_sse(r)
    metas = [d for t, d in events if t == "data" and isinstance(d, dict) and "intent" in d]
    if not metas:
        print("ERROR: no meta events")
        return 1
    meta = metas[0]
    xr = meta.get("xianyu_refund")
    print(f"meta.intent={meta.get('intent')!r}, xianyu_refund={xr!r}")
    # 商城路径不应有 xianyu_refund
    if xr is None:
        print("\n[REGRESSION PASS] 商城 channel 没串到 xianyu refund bridge")
        return 0
    print("\n[REGRESSION FAIL] 商城 channel 误触 xianyu_refund bridge")
    return 1


def main() -> int:
    print("[V16.8 live smoke] /api/chat via curl")
    failures = []
    for name, fn in [
        ("P0-C chitchat", p0c_chitchat),
        ("P0-A refund channel", p0a_refund_channel),
        ("regression web channel", regression_web_channel),
    ]:
        try:
            rc = fn()
            if rc != 0:
                failures.append(name)
        except AssertionError as e:
            print(f"\n[{name} FAIL] {e}")
            failures.append(name)
    if failures:
        print(f"\n[OVERALL FAIL] {failures}")
        return 1
    print("\n" + "=" * 60)
    print("[ALL PASS] V16.8 P0 live verification")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
