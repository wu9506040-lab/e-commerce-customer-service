"""
V16.3.3 XianyuItemRepository 测试 — 按 item_id 路由商品话术
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models import xianyu_tables  # noqa


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


# ============================================================
# 1. 按 item_id 查商品话术
# ============================================================
class TestGetItemPrompt:
    def test_get_existing_item(self, db_session):
        from app.models.xianyu_tables import XianyuItem
        from app.services.xianyu.xianyu_item_repository import XianyuItemRepository
        db_session.add(XianyuItem(
            item_id="945237003804",
            title="注塑模具设计修模改模打火花",
            item_prompt="材质：45 号钢；硬度 HRC58-62；价格 ¥10 起",
        ))
        db_session.commit()
        repo = XianyuItemRepository(db=db_session)
        prompt = repo.get_item_prompt("945237003804")
        assert "45 号钢" in prompt
        assert "HRC58-62" in prompt

    def test_get_nonexistent_item_returns_empty(self, db_session):
        from app.services.xianyu.xianyu_item_repository import XianyuItemRepository
        repo = XianyuItemRepository(db=db_session)
        prompt = repo.get_item_prompt("NOT_EXIST")
        assert prompt == ""

    def test_multiple_items_dont_mix(self, db_session):
        """A 商品的话术不能串到 B 商品"""
        from app.models.xianyu_tables import XianyuItem
        from app.services.xianyu.xianyu_item_repository import XianyuItemRepository
        db_session.add(XianyuItem(
            item_id="A",
            title="商品A",
            item_prompt="商品A的专属话术：硬度 HRC60",
        ))
        db_session.add(XianyuItem(
            item_id="B",
            title="商品B",
            item_prompt="商品B的专属话术：钛合金",
        ))
        db_session.commit()
        repo = XianyuItemRepository(db=db_session)
        a = repo.get_item_prompt("A")
        b = repo.get_item_prompt("B")
        # A 不能有 B 的话术
        assert "钛合金" not in a
        assert "HRC60" not in b

    def test_upsert(self, db_session):
        """upsert 行为：存在更新，不存在新建"""
        from app.models.xianyu_tables import XianyuItem
        from app.services.xianyu.xianyu_item_repository import XianyuItemRepository
        repo = XianyuItemRepository(db=db_session)
        repo.upsert(item_id="X1", title="t1", item_prompt="p1")
        repo.upsert(item_id="X1", title="t1-v2", item_prompt="p2")
        # 应只有 1 条记录
        count = db_session.query(XianyuItem).filter_by(item_id="X1").count()
        assert count == 1
        assert repo.get_item_prompt("X1") == "p2"
