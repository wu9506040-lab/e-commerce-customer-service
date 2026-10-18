"""
V16.3.3 init 脚本测试 — 灌接单服务话术
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models import xianyu_tables  # noqa
from app.services.xianyu.xianyu_item_repository import XianyuItemRepository


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


def test_upsert_with_service_fields(db_session):
    """upsert 带 service_type/requires_consultation/quote_basis"""
    repo = XianyuItemRepository(db=db_session)
    item = repo.upsert(
        item_id="945237003804",
        title="注塑模具接单服务",
        item_prompt="材料 45 号钢...",
        service_type="custom_order",
        requires_consultation=True,
        typical_questions=["你做 718 吗？", "打火花精度？"],
        quote_basis="project",
    )
    db_session.commit()
    # 验证字段
    assert item.service_type == "custom_order"
    assert item.requires_consultation is True
    assert item.quote_basis == "project"
    assert "你做 718 吗？" in item.typical_questions
    # 验证取回
    prompt = repo.get_item_prompt("945237003804")
    assert "45 号钢" in prompt

def test_buyer_profile_customer_type(db_session):
    """V16.3.3 BuyerProfile 客户类型字段"""
    from app.models.xianyu_tables import XianyuBuyerProfile
    from app.services.xianyu.xianyu_buyer_profile_v2 import XianyuBuyerProfileV2
    bp = XianyuBuyerProfileV2(db=db_session)
    bp.set_customer_type(
        unb="u1", customer_type="engineer",
        project_category="注塑模具", communication_pref="tech_first",
    )
    db_session.commit()
    found = db_session.query(XianyuBuyerProfile).filter_by(unb="u1").first()
    assert found.customer_type == "engineer"
    assert found.project_category == "注塑模具"
    assert found.communication_pref == "tech_first"
