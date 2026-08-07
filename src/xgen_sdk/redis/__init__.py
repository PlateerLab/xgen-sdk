"""xgen_sdk.redis — Redis 접속의 **단일 진입점**.

두 층으로 되어 있다.

    RedisSettings / create_*_redis   접속을 만드는 저수준 팩토리.
                                     Sentinel · URL · 직결을 여기서만 판단한다.
    RedisClient / get_redis_client   싱글턴 고수준 클라이언트 (세션·카탈로그
                                     캐시 등). 내부적으로 위 팩토리를 쓴다.

왜 한 곳으로 모았나 (2026-08-06 감사): ``service/redis_sentinel.py`` 가
xgen-core · xgen-workflow · xgen-documents 세 저장소에 **복사본**으로 있었고
도입 7일 만에 갈라졌다(core 사본에는 async 변형이 없고 타임아웃도 달랐다).
게다가 SDK 자신은 Sentinel 을 몰라서, 페일오버 뒤 서비스는 새 마스터를
따라가는데 SDK 만 강등된 옛 노드에 붙어 쓰기가 실패했다.

    from xgen_sdk.redis import create_sync_redis, create_async_redis, RedisSettings

    client = create_sync_redis(db=3, decode_responses=True)       # 환경변수 기반
    client = create_sync_redis(settings=RedisSettings(host="…"))  # 명시 설정
"""

from xgen_sdk.redis.settings import DIRECT_ENV, SENTINEL_ENV, RedisSettings
from xgen_sdk.redis.factory import (
    RedisNotConfiguredError,
    create_async_redis,
    create_sync_redis,
    ping_async,
    ping_sync,
)
from xgen_sdk.redis.client import RedisClient, get_redis_client

__all__ = [
    # 저수준 팩토리
    "RedisSettings",
    "SENTINEL_ENV",
    "DIRECT_ENV",
    "RedisNotConfiguredError",
    "create_sync_redis",
    "create_async_redis",
    "ping_sync",
    "ping_async",
    # 고수준 싱글턴 (기존 공개 API — 절대 빼면 안 된다)
    "RedisClient",
    "get_redis_client",
]
