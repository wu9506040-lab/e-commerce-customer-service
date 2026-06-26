"""
Redis 客户端封装

按 §6 规则：clients/ 层只做连接，不写业务逻辑。
被 services/redis_store.py 调用做会话存储。

注意：使用同步 redis-py，通过 asyncio.to_thread 在 FastAPI 中异步化。
"""
import logging
from typing import Optional

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

# =============================================================
# 配置
# =============================================================
REDIS_URL = settings.REDIS_URL

# 单例
_client: Optional[redis.Redis] = None


# =============================================================
# 连接
# =============================================================
def get_client() -> redis.Redis:
    """获取 Redis 客户端（单例）"""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,  # 自动 decode 为 str，方便 JSON 序列化
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
        # 启动时 ping 一次确认连接
        try:
            _client.ping()
            logger.info(f"redis client 初始化: {REDIS_URL}")
        except Exception:
            logger.exception(f"redis 连接失败: {REDIS_URL}")
            raise
    return _client


def close_client():
    """关闭连接（测试或优雅停机时用）"""
    global _client
    if _client is not None:
        try:
            _client.close()
        finally:
            _client = None