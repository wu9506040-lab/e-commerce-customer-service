"""
ConversationRecorder — 闲鱼会话消息实时落库（V18.2 人工后台核心）

背景：此前闲鱼消息只有"AI 回复完成后"才由 /chat 写穿落库——买家消息本体、
接管期间的消息、人工发的消息都不在 MySQL 里 → 人工工作台看不到对话内容。

职责（daemon 侧，收到即落库）：
  - record_buyer(cid, toid, nick, text, message_id)  买家消息（message_id 去重）
  - record_ai(cid, text)                             AI 回复（reply_sent 时）
  - record_human(cid, text, agent)                   人工回复（后台直发时）

落库格式（messages 表，session_id = xianyu-{cid}）：
  role=user      intent_snapshot={"channel": "xianyu"}
  role=assistant intent_snapshot={"channel": "xianyu"}                      ← AI
  role=assistant intent_snapshot={"channel": "xianyu", "human": True, ...} ← 人工

线程安全：每次写入独立 Session；内部去重集合有界（500）。
"""
from __future__ import annotations

import logging
import threading
from collections import deque

logger = logging.getLogger(__name__)


class ConversationRecorder:
    def __init__(self, session_factory):
        self._sf = session_factory
        self._seen_buyer: deque[str] = deque(maxlen=500)
        self._seen_set: set[str] = set()
        self._lock = threading.Lock()

    def _insert(self, session_id: str, user_id: int, role: str,
                content: str, snapshot: dict, account: str = 'a1') -> bool:
        try:
            session = self._sf()
        except Exception as e:
            logger.warning(f"recorder 获取 DB 会话失败: {e}")
            return False
        try:
            from app.models.message import Message
            session.add(Message(
                session_id=session_id,
                user_id=user_id,
                role=role,
                content=content,
                intent_snapshot=snapshot,
                account=account,
            ))
            session.commit()
            return True
        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass
            logger.warning(f"recorder 落库失败: {e}")
            return False
        finally:
            session.close()

    @staticmethod
    def _uid(toid: str) -> int:
        t = str(toid or "")
        return int(t) if t.isdigit() else 0

    def record_buyer(self, cid: str, toid: str, nick: str,
                     text: str, message_id: str = "", account: str = 'a1') -> bool:
        """买家消息（message_id 去重，防服务端重推重复落库）"""
        if not text:
            return False
        with self._lock:
            if message_id and message_id in self._seen_set:
                return False
            if message_id:
                self._seen_set.add(message_id)
                self._seen_buyer.append(message_id)
        ok = self._insert(
            f"xianyu-{cid}", self._uid(toid), "user", text,
            {"channel": "xianyu", "buyer_nick": nick or ""},
            account=account,
        )
        if ok:
            logger.info(f"recorder 买家消息落库 cid={cid} len={len(text)}")
        return ok

    def record_ai(self, cid: str, text: str, account: str = 'a1') -> bool:
        return self._insert(
            f"xianyu-{cid}", 0, "assistant", text,
            {"channel": "xianyu", "ai": True},
            account=account,
        )

    def record_human(self, cid: str, text: str, agent: str = "", account: str = 'a1') -> bool:
        return self._insert(
            f"xianyu-{cid}", 0, "assistant", text,
            {"channel": "xianyu", "human": True, "agent": agent},
            account=account,
        )

    # V18.2.1：订单状态证据（买家消息里的交易卡片文本）
    _ORDER_STAGE_PATTERNS = [
        ("refunded", ("退款成功", "已退款", "钱款已原路退返")),
        ("shipped", ("卖家已发货", "已发货", "物流已签收", "已签收")),
        ("paid", ("我已付款", "待发货", "等待你发货")),
        ("pending_payment", ("待付款", "我已拍下")),
    ]

    def lookup_order_evidence(self, cid: str, scan_limit: int = 300) -> dict:
        """扫描该会话落库消息里的交易卡片痕迹 → 判断买家是否真的下过单

        依据：闲鱼交易状态以黄色卡片消息出现在会话里（"我已付款，等待你发货"
        /"卖家已发货"/"退款成功"等），recorder 已把这些消息落库。
        Returns: {"has_order": bool, "stage": str|None, "evidence": [..],
                  "recent_history": [..最近6条..]}
        """
        result: dict = {"has_order": False, "stage": None, "evidence": [],
                        "recent_history": []}
        try:
            session = self._sf()
        except Exception as e:
            logger.warning(f"lookup_order_evidence 会话失败: {e}")
            return result
        try:
            from app.models.message import Message
            rows = (
                session.query(Message)
                .filter(Message.session_id == f"xianyu-{cid}")
                .order_by(Message.id.desc())
                .limit(scan_limit)
                .all()
            )
            rows = list(reversed(rows))  # 时间正序
            # 交易阶段：按流程最远阶段判定（付过款又退款 → refunded 优先呈现）
            for stage, kws in self._ORDER_STAGE_PATTERNS:
                for r in rows:
                    for kw in kws:
                        if kw in (r.content or ""):
                            result["has_order"] = True
                            result["stage"] = stage
                            result["evidence"].append(
                                f"{stage} ← {str(r.create_time)[:16]} {kw}"
                            )
                            break
                    if result["stage"] == stage:
                        break
            result["recent_history"] = [
                f"{'买' if r.role == 'user' else '客服'}: {(r.content or '')[:60]}"
                for r in rows[-6:]
            ]
        except Exception as e:
            logger.warning(f"lookup_order_evidence 失败: {e}")
        finally:
            session.close()
        return result


__all__ = ["ConversationRecorder"]
