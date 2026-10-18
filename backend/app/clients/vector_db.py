"""
vector_db 门面（V18.3）— 按 settings.VECTOR_DB 转发到 Qdrant 或 Milvus

迁移期双栈并存：调用方把 `from app.clients.qdrant import X` 改为
`from app.clients.vector_db import X`，其余零改动。切换后端只需
改 .env 的 VECTOR_DB=qdrant|milvus 重启。
"""
import importlib
from typing import Any, Dict, List, Optional

from app.core.config import settings

_BACKEND = (settings.VECTOR_DB or "qdrant").strip().lower()
_mod = importlib.import_module(f"app.clients.{_BACKEND}")

QDRANT_COLLECTION = _mod.QDRANT_COLLECTION
VECTOR_SIZE = getattr(_mod, "VECTOR_SIZE", 1024)

ensure_collection = _mod.ensure_collection
upsert_points = _mod.upsert_points
search = _mod.search
delete_points = _mod.delete_points
delete_source_points = _mod.delete_source_points
get_collection_info = _mod.get_collection_info
health_check = _mod.health_check
get_client = _mod.get_client
vector_db_breaker = _mod.vector_db_breaker  # 统一断路器名（main /health 用）


def backend_name() -> str:
    return _BACKEND


__all__ = ["backend_name", "vector_db_breaker", "QDRANT_COLLECTION", "VECTOR_SIZE",
           "ensure_collection", "upsert_points", "search", "delete_points",
           "delete_source_points", "get_collection_info", "health_check",
           "get_client"]
