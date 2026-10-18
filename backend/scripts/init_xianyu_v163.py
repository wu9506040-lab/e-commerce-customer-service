"""
V16.3.3 初始化脚本：灌闲鱼 B2B 接单服务话术

跑一次：python -m scripts.init_xianyu_v163
"""
import sys
from pathlib import Path

# 注入路径
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.base import Base
from app.models import xianyu_tables  # noqa - 注册
from app.services.xianyu.xianyu_item_repository import XianyuItemRepository


# V16.3.3 B2B 闲鱼接单服务话术
ITEM_PROMPTS = {
    "945237003804": {
        "title": "注塑模具设计·修模·改模·打火花（EDM）",
        "service_type": "custom_order",  # 接单服务（非普通商品）
        "requires_consultation": True,   # 必须先咨询
        "quote_basis": "project",         # 按项目包干
        "typical_questions": [
            "你这边能做 718 材料吗？",
            "打火花精度多少？±0.01 能做吗？",
            "改模要几天？图纸发你微信？",
            "啤 ABS 缩水多少？",
            "能开 13% 专票吗？",
        ],
        "item_prompt": """
【服务类型：注塑模具接单（设计·修模·改模·打火花 EDM）】
⚠️ 这是接单链接，不是库存商品。买家咨询后才报工时/项目价。

服务范围：
- 模具设计：3D 分模/DFM 报告/模流分析
- 修模：飞边/缺料/粘模/拉伤 修复
- 改模：换型/加减镶件/局部修改
- 打火花 EDM：深窄槽/盲孔/高硬度钢
- 试模：试料/试模跟踪

材料/工艺参数：
- 材料：45 号钢（默认）/ P20 / 718 / S136
- 硬度：HRC 58-62
- 交期：3-15 天（按复杂度）
- 精度：±0.02mm 标准 / ±0.01mm 精磨

回复规则：
1. **不直接报价**——要看到图纸/3D 才能报
2. **引导提供资料**：产品 3D/2D 图、啤塑材料、年需求量
3. **不催促下单**——买家是技术决策，慢慢聊
4. **专业感**比议价重要
""",
    },
}


def main() -> None:
    """灌 V16.3.3 闲鱼接单服务话术到 xianyu_items 表"""
    engine = create_engine(settings.DATABASE_URL)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        repo = XianyuItemRepository(db=session)
        for item_id, cfg in ITEM_PROMPTS.items():
            item = repo.upsert(
                item_id=item_id,
                title=cfg["title"],
                item_prompt=cfg["item_prompt"],
                service_type=cfg["service_type"],
                requires_consultation=cfg["requires_consultation"],
                typical_questions=cfg.get("typical_questions"),
                quote_basis=cfg.get("quote_basis", ""),
            )
            print(
                f"✅ 灌 {item_id}: {item.title} | "
                f"service_type={item.service_type} | "
                f"quote={item.quote_basis}"
            )
        session.commit()
        print(f"\n共 {len(ITEM_PROMPTS)} 个接单服务话术已落库")
    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
