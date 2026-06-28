"""
ORM Models - SQLAlchemy 2.0 Declarative

对应 MySQL schema（deploy/mysql/init/01_schema.sql）
启动时由 app.main 调 Base.metadata.create_all 建表（仅在表不存在时）
"""
from app.models.base import Base
from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.knowledge_document import KnowledgeDocument
from app.models.operation_log import OperationLog
from app.models.product import Product
from app.models.order import Order, OrderItem, OrderStatus
from app.models.refund import Refund, RefundStatus

__all__ = [
    "Base",
    "User",
    "Conversation",
    "Message",
    "KnowledgeDocument",
    "OperationLog",
    "Product",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Refund",
    "RefundStatus",
]