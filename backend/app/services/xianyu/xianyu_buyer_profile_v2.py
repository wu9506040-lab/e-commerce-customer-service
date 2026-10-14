"""
V16.3.3 XianyuBuyerProfile V2 — 7 天 TTL + 上下文摘要

设计与 V16.1 BuyerProfile 的区别：
- V16.1: TTL 5min（短）→ 同一会话内缓存
- V16.3.3: TTL 7 天（长）→ 买家隔天回来要记得

上下文压缩：
- 早期消息 → LLM 摘要 1 段（保语义降 token）
- 近期消息 → 原样保留
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.xianyu_tables import XianyuBuyerProfile, XianyuConversation

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 7


class XianyuBuyerProfileV2:
    """闲鱼买家画像 V2（7 天 TTL，独立表）"""

    def __init__(self, db: Session):
        self._db = db

    def get_or_create(self, unb: str, nick: str = "") -> XianyuBuyerProfile:
        """查买家画像（7 天 TTL）；不存在则建"""
        p = self._db.query(XianyuBuyerProfile).filter_by(unb=unb).first()
        if p:
            # 7 天内 → 用缓存
            if p.updated_at and (datetime.datetime.now() - p.updated_at).days < CACHE_TTL_DAYS:
                return p
            # 超 7 天 → 更新
            p.nick = nick or p.nick
            p.last_message_at = datetime.datetime.now()
            p.updated_at = datetime.datetime.now()
            self._db.commit()
            return p
        # 新建
        p = XianyuBuyerProfile(
            unb=unb, nick=nick, is_returning_buyer=False,
            created_at=datetime.datetime.now(),
            updated_at=datetime.datetime.now(),
        )
        self._db.add(p)
        self._db.commit()
        return p

    def mark_returning(self, unb: str) -> None:
        """标老客"""
        p = self.get_or_create(unb)
        p.is_returning_buyer = True
        p.last_message_at = datetime.datetime.now()
        p.updated_at = datetime.datetime.now()
        self._db.commit()

    def update_last_topic(self, unb: str, topic: str) -> None:
        """记录最近咨询主题（议价/物流/退款/材质 等）"""
        p = self.get_or_create(unb)
        p.last_query_topic = topic[:64]
        p.updated_at = datetime.datetime.now()
        self._db.commit()

    def update_summary(self, unb: str, summary: str) -> None:
        """更新上下文摘要（压缩后的早期消息）"""
        p = self.get_or_create(unb)
        p.conversation_summary = summary[:2000]
        p.updated_at = datetime.datetime.now()
        self._db.commit()

    def has_order(self, unb: str, cid: str) -> bool:
        """该买家在该会话里是否真有订单（按 tip 流）"""
        # tip_type in (paid, shipped, signed, received) 视为有订单
        return self._db.query(XianyuConversation).filter(
            XianyuConversation.unb == unb,
            XianyuConversation.cid == cid,
            XianyuConversation.is_tip == True,
            XianyuConversation.tip_type.in_(["paid", "shipped", "signed", "received"]),
        ).first() is not None

    def build_context(
        self,
        unb: str,
        cid: str,
        current_query: str,
        max_history: int = 20,
        max_tokens_estimate: int = 2000,
    ) -> dict:
        """构建 LLM context：BuyerProfile + 历史摘要 + 近期消息

        返回：{
            "profile_summary": "买家昵称=X，老客，是/否，最近问过...",
            "history": [{"role": "buyer"|"seller", "text": "..."}...],
            "has_order": True/False,
        }
        """
        p = self.get_or_create(unb)
        msgs = self._db.query(XianyuConversation).filter_by(
            unb=unb, cid=cid
        ).order_by(XianyuConversation.created_at).all()

        # 分：早期（要摘要）+ 近期（原样）
        if len(msgs) > max_history:
            older = msgs[:-max_history // 2]
            recent = msgs[-max_history // 2:]
        else:
            older = []
            recent = msgs

        # 早期消息 → 摘要（保语义）
        summary = p.conversation_summary or ""
        if older and not summary:
            # 首次压缩
            summary = self._summarize_older(older)
            self.update_summary(unb, summary)

        return {
            "nick": p.nick or "",
            "is_returning": p.is_returning_buyer,
            "last_topic": p.last_query_topic or "",
            "summary": summary,
            "recent": [
                {"role": m.role, "text": m.message_text or m.tip_type or ""}
                for m in recent
            ],
            "has_order": self.has_order(unb, cid),
            "current_query": current_query,
        }

    def _summarize_older(self, older_messages) -> str:
        """LLM 摘要早期消息（占位：实际用 LLM）"""
        # 简单摘要（生产用 LLM）
        topics = [m.message_text[:20] for m in older_messages if m.message_text]
        return f"（历史）买家问过：" + " / ".join(topics[:10])


__all__ = ["XianyuBuyerProfileV2"]
