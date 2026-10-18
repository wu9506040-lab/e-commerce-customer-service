"""V16.8 P2-A probe v2：用正确 MTOP API 测试 HTTP 拉数据

正确 API: mtop.taobao.idlemessage.pc.session.sync (version=3.0)

用法：cd backend && python probe_mtop_poller.py
"""
from __future__ import annotations
import json
import sys

sys.path.insert(0, r"C:/Users/zw/AppData/Local/Packages/PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0/LocalCache/local-packages/Python311/site-packages")

from goofish_cli.core import Session
from goofish_cli.core.mtop import call


def probe():
    print("=" * 60)
    print("[probe] 加载 Session")
    print("=" * 60)
    session = Session.load()
    print(f"  unb = {session.unb}")
    print(f"  h5_token length = {len(session.h5_token or '')}")
    print()

    # probe 1: 拉会话基线（用正确 API + version=3.0 + spm_cnt="a21ybx.im.0.0"）
    print("=" * 60)
    print("[probe 1] mtop.taobao.idlemessage.pc.session.sync v3.0 fetchNum=20")
    print("=" * 60)
    sessions = []
    try:
        raw = call(
            session,
            api="mtop.taobao.idlemessage.pc.session.sync",
            data={"fetchNum": 20},
            version="3.0",
            spm_cnt="a21ybx.im.0.0",
        )
        print(f"  top keys = {list(raw.keys())}")
        data = raw.get("data") or {}
        print(f"  data keys = {list(data.keys())}")
        sessions = data.get("sessions") or []
        print(f"  sessions count = {len(sessions)}")
        if sessions:
            print(f"  first session keys = {list(sessions[0].keys()) if isinstance(sessions[0], dict) else type(sessions[0])}")
            s0 = sessions[0]
            print(f"  first session (前 800 chars): {json.dumps(s0, ensure_ascii=False)[:800]}")
            print(f"  hasMore = {data.get('hasMore')}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
    print()

    # probe 2: 从第一个 session 拉消息列表（找具体消息 API）
    if sessions:
        first = sessions[0]
        cid = first.get("sessionId") or first.get("cid") or first.get("session_id")
        print("=" * 60)
        print(f"[probe 2] 用 cid={cid} 拉消息历史")
        print("=" * 60)
        # 试 mtop.idle.message.individualpage.getchatdata v=1.0
        try:
            raw2 = call(
                session,
                api="mtop.idle.message.individualpage.getchatdata",
                data={
                    "cid": cid,
                    "pageSize": 5,
                    "beginMessageId": "",  # 从最新开始
                },
                spm_cnt="a21ybx.im.0.0",
            )
            print(f"  top keys = {list(raw2.keys())}")
            data2 = raw2.get("data") or {}
            print(f"  data keys = {list(data2.keys())}")
            msgs = (
                data2.get("userMessageModels")
                or data2.get("messages")
                or data2.get("messageModels")
                or data2.get("messagesV2")
                or []
            )
            print(f"  msgs count = {len(msgs) if isinstance(msgs, list) else 'NOT LIST'}")
            if isinstance(msgs, list) and msgs:
                print(f"  first msg = {json.dumps(msgs[0], ensure_ascii=False)[:600]}")
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
        print()

    # probe 3: 用另一个 candidate — taobao.idle.pc.loginuser.get
    print("=" * 60)
    print("[probe 3] test alternative: mtop.idle.account.userinfobatch.query")
    print("=" * 60)
    try:
        raw3 = call(session, "mtop.idle.account.userinfobatch.query",
                    {"uids": str(session.unb)}, spm_cnt="a21ybx.0.0")
        print(f"  ret = {raw3.get('ret', raw3)[:200]}")
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    probe()
