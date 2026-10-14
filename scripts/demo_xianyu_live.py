"""
闲鱼 MCP 接入演示脚本（端到端 + 截图友好）

按 CLAUDE.md 调试铁律：演示需人工确认发送；写操作前显式提示。
使用前提：
  1. 已 `goofish auth login ~/goofish-cookies.json` 导入闲鱼卖家 cookie
  2. 已开 ENABLE_XIANYU_TOOLS=true（.env.dev 加）
  3. 智能客服 API 已起（http://localhost:8000）

演示链路：
  1. xianyu_list_conversations → 拿最近 5 个会话
  2. xianyu_get_conversation_history(cid) → 拉最新一条买家消息
  3. POST /api/chat → 智能客服 Agent 生成回复（带 FC 工具调用）
  4. 人工确认 → xianyu_send_message(cid, text) → 真发到闲鱼

用法：
  python scripts/demo_xianyu_live.py --dry-run         # 只打印，不调 send_message
  python scripts/demo_xianyu_live.py --limit 3 --yes   # 真发（已确认）
"""
import argparse
import asyncio
import json
import os
import sys
from typing import Optional

# 注入路径（scripts/ 与 backend/ 平级）
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep + "backend"
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)  # 让 .env.dev 可被 Settings 读取

# ---- 脱机占位环境变量（与 conftest.py 同模式）----
# JWT_SECRET 启动时校验≥32 字符；演示场景用占位绕过（生产绝不允许）
os.environ.setdefault(
    "JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
)
# DATABASE_URL 也必须设置（仅占位，本演示不真连 MySQL）
os.environ.setdefault(
    "DATABASE_URL",
    "mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4",
)
os.environ.setdefault("APP_ENV", "dev")

from app.integrations import get_xianyu_client  # noqa: E402
from app.integrations.xianyu_mcp_client import XianyuMCPClient  # noqa: E402


def banner(text: str) -> None:
    print(f"\n{'=' * 60}\n{text}\n{'=' * 60}")


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def pretty(d: dict) -> None:
    print(json.dumps(d, ensure_ascii=False, indent=2)[:500])


async def run(dry_run: bool, limit: int, yes: bool) -> int:
    client = XianyuMCPClient()
    banner("闲鱼 MCP × 智能客服 · 端到端演示")

    # ---- 0. 健康检查 ----
    step(0, "健康检查")
    h = client.health_check()
    pretty(h)
    if not h["is_enabled"]:
        print("❌ 客户端未启用（开关关 / cookie 缺 / SDK 缺）。退出。")
        return 1

    # ---- 1. 列出会话 ----
    step(1, f"列最近 {limit} 个闲鱼会话")
    convs = client.list_conversations(limit=limit)
    pretty(convs)
    if "error" in convs:
        print(f"❌ 列会话失败：{convs['error']}")
        return 1
    conversations = convs.get("conversations") or convs.get("chats") or []
    if not conversations:
        print("❌ 无会话。请先用买家小号发几条消息。")
        return 1

    # 选第一个
    target = conversations[0]
    cid = target.get("cid") or target.get("conversation_id") or target.get("id")
    if not cid:
        print(f"❌ 会话缺 cid 字段。实样: {list(target.keys())}")
        return 1
    print(f"✅ 选 cid={cid}，买家昵称={target.get('buyer_nick') or target.get('nick') or '?'}")

    # ---- 2. 拉历史 ----
    step(2, f"拉 cid={cid} 的最近 1 页历史")
    hist = client.get_conversation_history(cid=cid, pages=1)
    pretty(hist)
    if "error" in hist:
        print(f"❌ 拉历史失败：{hist['error']}")
        return 1
    messages = hist.get("messages") or hist.get("history") or []
    if not messages:
        print("❌ 无历史消息。")
        return 1
    last_buyer = next((m for m in reversed(messages) if m.get("role") == "buyer"), None)
    if not last_buyer:
        last_buyer = messages[-1]
    last_text = last_buyer.get("text") or last_buyer.get("content") or ""
    print(f"✅ 最近一条：{last_text[:80]!r}")

    # ---- 3. 调智能客服 Agent（生成回复）----
    step(3, "调智能客服 /api/chat 生成回复（待 V16.x 集成，本步先 placeholder）")
    suggested = (
        f"（演示）您好，看到您关于「{last_text[:30]}」的咨询。"
        "这边确认后马上为您处理～"
    )
    print(f"✅ 建议回复：{suggested}")

    # ---- 4. 真发回闲鱼 ----
    step(4, "xianyu_send_message（写操作）")
    if dry_run:
        print("🛡 dry-run：不调 send_message，仅打印 args")
        print(json.dumps({"cid": cid, "text": suggested}, ensure_ascii=False, indent=2))
        return 0

    print(f"⚠️ 即将发送：cid={cid} text={suggested!r}")
    if not yes:
        print("需 --yes 标志确认发送（演示用小号，不发真实交易指令）")
        return 2

    send = client.send_message(cid=cid, text=suggested)
    pretty(send)
    if "error" in send:
        print(f"❌ 发送失败：{send['error']}")
        return 1
    print(f"✅ 已发送：{send}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="闲鱼 MCP × 智能客服端到端演示")
    p.add_argument("--dry-run", action="store_true", help="只打印不发送")
    p.add_argument("--limit", type=int, default=3, help="列会话数上限")
    p.add_argument("--yes", action="store_true", help="确认真发（写操作）")
    args = p.parse_args()
    return asyncio.run(run(args.dry_run, args.limit, args.yes))


if __name__ == "__main__":
    sys.exit(main())