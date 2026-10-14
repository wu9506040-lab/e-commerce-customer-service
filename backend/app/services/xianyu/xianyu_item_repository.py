"""
V16.3.3 XianyuItemRepository — 按 item_id 路由商品话术

设计：
- 通用话术共用（开场白/礼貌语）
- 商品话术按 item_id 精确加载（xianyu_items 表）
- 多商品时**不混**——每个会话只用自己的商品话术
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.xianyu_tables import XianyuItem

logger = logging.getLogger(__name__)


class XianyuItemRepository:
    """闲鱼商品话术仓库（V16.3.3）"""

    def __init__(self, db: Session):
        self._db = db

    def get_item_prompt(self, item_id: str) -> str:
        """按 item_id 查商品话术

        找不到返空字符串（不报错——fallback 到通用话术）
        """
        if not item_id:
            return ""
        item = self._db.query(XianyuItem).filter_by(item_id=item_id).first()
        if not item:
            logger.debug(f"XianyuItemRepository 未找到 item_id={item_id}")
            return ""
        return item.item_prompt or ""

    def get_item_title(self, item_id: str) -> str:
        """按 item_id 查商品标题（用于 buyer profile 显示）"""
        if not item_id:
            return ""
        item = self._db.query(XianyuItem).filter_by(item_id=item_id).first()
        if not item:
            return ""
        return item.title or ""

    def upsert(
        self,
        item_id: str,
        title: str = "",
        item_prompt: str = "",
        extra: Optional[dict] = None,
    ) -> XianyuItem:
        """upsert：存在更新，不存在新建"""
        item = self._db.query(XianyuItem).filter_by(item_id=item_id).first()
        if item:
            if title:
                item.title = title
            if item_prompt:
                item.item_prompt = item_prompt
            if extra is not None:
                item.extra = extra
            self._db.commit()
            return item
        # 新建
        item = XianyuItem(
            item_id=item_id, title=title, item_prompt=item_prompt, extra=extra
        )
        self._db.add(item)
        self._db.commit()
        return item

    def list_all(self) -> list[XianyuItem]:
        """列所有商品（admin 用）"""
        return self._db.query(XianyuItem).all()


__all__ = ["XianyuItemRepository"]
