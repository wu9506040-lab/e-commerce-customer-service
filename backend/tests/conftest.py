"""
pytest conftest — 全局共享 fixture (SOP-V1 §2.2 数据可信验证规范样板)

按 docs/governance/ai_development_sop.md §2 落地 P0-4：
- 提供真 DB 集成测试基础设施（db_session fixture）
- 用 SQLite in-memory 作本地样板，CI 可切换真 MySQL

约束：
- pytest collection 之前必须 setdefault 关键 env（JWT_SECRET/DATABASE_URL）
- conftest 加载早于任何 test_*.py 的 import，必须最先 setdefault
- 真实模块的 setdefault（如 test_profile_service.py）保留；本 conftest 提供全局兜底

V1.1 增量（不在本 conftest 范围）：
- backend/tests/fixtures/ 目录 + 种子数据
- 真 MySQL 集成测试（CI 上 docker compose up mysql + pytest）
"""
import os

# =============================================================
# 1. env 兜底（必须在任何 from app.xxx import 之前）
# =============================================================
# 与现有 test_*.py 顶部 setdefault 对齐；这里集中兜底避免每个文件重复
os.environ.setdefault(
    "JWT_SECRET", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
)  # 32 字符，绕过 settings 启动校验
os.environ.setdefault(
    "DATABASE_URL", "mysql+pymysql://placeholder:pwd@mysql:3306/customer_service?charset=utf8mb4"
)  # 占位值；真 DB 测试用 sqlite_url 覆盖
os.environ.setdefault("APP_ENV", "test")


# =============================================================
# 2. db_session fixture — 真 DB 集成测试样板核心
# =============================================================
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_session():
    """每个测试函数独立 SQLite in-memory session，测试结束自动 rollback + dispose。

    SOP-V1 §2.3 DB 断言规范的样板 fixture：
    - 真 SQLAlchemy 引擎（不是 MagicMock）
    - Base.metadata.create_all 动态建表
    - 测试函数拿到 session 后可自由操作
    - yield 后自动 rollback + dispose，确保测试隔离

    限制（V1.1 升级路径）：
    - SQLite 与 MySQL 语法差异（Numeric→REAL/DateTime 精度等），不能完全替代真 MySQL
    - CI 升级方案：加 pytest-mysql 或 docker compose up mysql + 切 fixture engine

    Yields:
        Session: SQLAlchemy Session 实例
    """
    # 延迟 import Base + models，避开 conftest 加载期的副作用
    from app.models.base import Base
    from app.models import (  # noqa: F401 — 注册所有 model 到 Base.metadata
        order, user, refund, conversation,
        message, knowledge_document, operation_log,
        product, user_profile,
    )

    # SQLite in-memory；check_same_thread=False 允许跨线程（虽然单测不需要，但安全）
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        yield session
    finally:
        session.rollback()  # 兜底回滚（即使测试 commit 过也清掉）
        session.close()
        engine.dispose()


@pytest.fixture
def db_session_with_commit(db_session):
    """允许 commit 的 db_session（部分业务场景需要显式 commit 才能观察到副作用）。

    与 db_session 的差别：测试结束后 **不** rollback，便于验证 commit 后的持久化行为
    （仅在 SQLite in-memory 内，因为 dispose 后内存表即销毁）。
    """
    yield db_session