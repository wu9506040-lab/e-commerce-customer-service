"""
MySQL 客户端封装 - SQLAlchemy 2.0 sync + PyMySQL

按 §6 规则：clients/ 层只做连接，被 services/ 调用
按现有 redis_client.py 风格：单例 + 懒加载 + sync SDK

为什么不用 async（aiomysql）：
- 与 redis_client 风格统一（同步 SDK + asyncio.to_thread 异步化）
- Auth/User 查询是快 IO，同步足够，async 收益小
- 改动最小化（DATABASE_URL 已经是 mysql+pymysql://，无需改 docker-compose.yml）
"""
import logging
from contextlib import contextmanager
from typing import Generator, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

# =============================================================
# 单例（懒加载）
# =============================================================
_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    """获取 SQLAlchemy Engine（单例，懒加载）"""
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_pre_ping=True,     # 每次借连接前 ping，断线自动重连
            pool_size=5,
            max_overflow=10,
            pool_recycle=3600,      # 1h 回收，防 MySQL wait_timeout 切断空闲连接
            echo=False,
        )
        # 启动时 ping 一次确认连接
        try:
            with _engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            logger.info(
                f"MySQL engine 初始化: ...@{settings.DATABASE_URL.split('@')[-1]}"
            )
        except Exception:
            logger.exception(f"MySQL 连接失败: {settings.DATABASE_URL}")
            raise
    return _engine


def get_session_local() -> sessionmaker:
    """获取 sessionmaker（单例，懒加载）"""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,  # commit 后仍可访问字段，避免 lazy load 报错
        )
    return _SessionLocal


# =============================================================
# FastAPI Dependency
# =============================================================
def get_db() -> Generator[Session, None, None]:
    """
    FastAPI Depends 注入 DB session

    用法:
        @router.post("/xxx")
        def endpoint(db: Session = Depends(get_db)):
            ...

    注意: sync session 在 async endpoint 里要包 asyncio.to_thread
    """
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def close_engine() -> None:
    """关闭连接池（测试或优雅停机时用）"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
        _engine = None
        _SessionLocal = None


# =============================================================
# Best-effort session 工具（替 5 处重复的"独立 session + try/except + rollback"）
# =============================================================
@contextmanager
def with_safe_session(commit: bool = True) -> Iterator[Session]:
    """
    创建一个独立 SQLAlchemy session，整个块被 try/except 包裹。

    设计目标：替换 services/ 层 5 处重复的"独立 session 写法"，
    行为与原 try/except/finally 完全一致：

    - yield 之前创建 session
    - yield 完（无异常）→ 视 commit 入参决定是否 commit()
    - 块内异常 → warning + rollback（吞咽），不抛出
    - finally 一定 close()

    Args:
        commit: True=块结束后 commit（写入场景）；False=不 commit（只读场景）

    Yields:
        SQLAlchemy Session

    用法:
        with with_safe_session(commit=True) as db:
            db.add(obj)
        # 自动 commit

        history = []
        with with_safe_session(commit=False) as db:
            rows = db.execute(...).all()
            history = [...]
        return history  # session 已 close
    """
    db = get_session_local()()
    try:
        yield db
        if commit:
            db.commit()
    except Exception as e:
        logger.warning(f"safe_session failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
