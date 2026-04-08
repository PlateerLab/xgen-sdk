"""
xgen_sdk.redis — Redis 클라이언트 모듈

세션 관리 및 범용 Redis 작업을 위한 싱글턴 클라이언트
"""

from xgen_sdk.redis.client import RedisClient, get_redis_client

__all__ = [
    "RedisClient",
    "get_redis_client",
]
