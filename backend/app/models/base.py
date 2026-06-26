"""SQLAlchemy 2.0 Declarative Base - 所有 ORM model 继承此类"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM model 继承此类"""
    pass
