"""V16.8 P2-A probe v3：探测 session.sync data 能拿多少字段

目标：搞清 session.message 字段到底有什么 → 决定 poller callback 传什么数据
"""
from __future__ import annotations
import base64
import json
import sys

sys.path.insert(0, r"C:/Users/zw/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0/LocalCache/local-packages/Python311/site-packages")

from goofish_cli.core import Session
from goofish_cli.core.mtop import call


def probe():
    session = Session.load()
    print(f"unb = {session.unb}")
    print()

    raw = call(
        session,
        api="mtop.taobao.idlemessage.pc.session.sync",
        data={"fetchNum": 20},
        version="3.0",
        spm_cnt="a21ybx.im.0.0",
    )
    sessions = (raw.get("data") or {}).get("sessions") or []
    print(f"sessions = {len(sessions)}")
    print()

    for i, s in enumerate(sessions[:3]):
        print(f"--- [{i}] session ---")
        print(f"  keys = {list(s.keys())}")
        msg = s.get("message") or {}
        print(f"  message keys = {list(msg.keys())}")
        # 重点看 summary 字段（goofish 用户发的消息摘要在这）
        summary = msg.get("summary") or {}
        print(f"  summary keys = {list(summary.keys())}")
        # ⚠️ 重要：看 actual type — bytes 还是 str？goofish 返回的可能是 GBK encoded bytes
        print(f"  summary type check:")
        for k in ["prefix", "summary", "ts", "unread", "sortIndex", "version"]:
            v = summary.get(k)
            if isinstance(v, (str, bytes)):
                type_name = type(v).__name__
                if isinstance(v, bytes):
                    # 多重 encoding 试探
                    decoded = None
                    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
                        try:
                            decoded = v.decode(enc)
                            break
                        except UnicodeDecodeError:
                            continue
                    print(f"    {k}: type={type_name} len={len(v)} → dec({enc or '?'})={decoded[:80]!r} hex={v[:20].hex()}")
                else:
                    print(f"    {k}: type={type_name} repr={v!r}")
        session_inner = s.get("session") or {}
        print(f"  session.sessionId = {session_inner.get('sessionId')}")
        print(f"  session.userInfo.nick = {session_inner.get('userInfo', {}).get('nick')}")
        print(f"  session.targetId = {session_inner.get('targetId')}")
        # message.messageList / messageDetail 看看是否有最新一条
        md = msg.get("messageDetail") or msg.get("messageList") or msg.get("latestMessage") or {}
        if md:
            print(f"  msg.messageDetail keys = {list(md.keys()) if isinstance(md, dict) else 'N/A'}")
            # 如果是 base64
            data = md.get("data") if isinstance(md, dict) else None
            if data:
                try:
                    decoded = base64.b64decode(data)
                    payload = json.loads(decoded)
                    print(f"  decoded payload = {json.dumps(payload, ensure_ascii=False)[:600]}")
                except Exception as e:
                    print(f"  decode fail: {e}")
        # 还有 attachment / extension ?
        for k in ["attachment", "extension", "content", "text", "sendMessage", "send_message"]:
            v = msg.get(k)
            if v:
                if isinstance(v, dict):
                    print(f"  msg.{k} keys = {list(v.keys())}")
                else:
                    print(f"  msg.{k} = {v!r}"[:200])
        print()


if __name__ == "__main__":
    probe()
