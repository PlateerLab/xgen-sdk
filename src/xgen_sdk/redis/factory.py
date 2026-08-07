"""Redis 클라이언트 팩토리 — sync/async · Sentinel/URL/직결을 한 곳에서.

서비스가 직접 ``redis.Redis(...)`` 를 부르지 않게 하는 것이 목적이다. 접속
방식(Sentinel 인지, URL 인지, host/port 인지)은 **배포 환경의 문제**이고
호출처가 알아야 할 일이 아니다. 호출처는 "어떤 db 를, 디코딩할지, 타임아웃을
얼마로" 만 고르면 된다.

    from xgen_sdk.redis import create_sync_redis, create_async_redis

    client = create_sync_redis(db=3, decode_responses=True)
    sub = await create_async_redis(socket_timeout=None)   # 블로킹 구독

접속 정보를 코드에서 명시하고 싶으면 설정을 직접 준다::

    from xgen_sdk.redis import RedisSettings, create_sync_redis

    settings = RedisSettings(host="10.0.0.5", port=6379, password="…", db=2)
    client = create_sync_redis(settings=settings)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from xgen_sdk.redis.settings import RedisSettings

logger = logging.getLogger("xgen-sdk.redis")

__all__ = [
    "RedisNotConfiguredError",
    "create_sync_redis",
    "create_async_redis",
    "ping_sync",
    "ping_async",
]


class RedisNotConfiguredError(RuntimeError):
    """접속 정보가 없다.

    기본값을 지어내 엉뚱한 서버에 붙는 것보다 **명확히 실패**하는 편이 낫다.
    Redis 가 선택 기능인 호출처는 이 예외를 잡아 메모리 모드로 내려가면 된다
    (:func:`create_sync_redis` 의 ``required=False`` 가 그걸 대신해 준다).
    """


def _resolve(settings: Optional[RedisSettings], overrides: dict) -> RedisSettings:
    base = settings if settings is not None else RedisSettings.from_env()
    # None 도 **의미 있는 값**이다 (socket_timeout=None = 무한 대기). 그래서
    # "주지 않은 것"과 "None 을 준 것"을 구분해 넘겨야 한다 — 호출부가 넘긴
    # 키만 골라낸다.
    return base.with_overrides(**overrides) if overrides else base


def create_sync_redis(
    *,
    settings: Optional[RedisSettings] = None,
    required: bool = True,
    **overrides: Any,
):
    """동기 Redis 클라이언트.

    Args:
        settings: 접속 설정. 생략하면 환경변수에서 읽는다.
        required: ``False`` 면 설정이 없을 때 예외 대신 ``None`` 을 준다 —
            Redis 가 선택 기능인 호출처(메모리 모드 폴백)를 위한 것이다.
        **overrides: ``db`` · ``decode_responses`` · ``socket_timeout`` 등
            :class:`RedisSettings` 의 필드.

    Raises:
        RedisNotConfiguredError: 설정이 없고 ``required=True`` 일 때.
    """
    cfg = _resolve(settings, overrides)
    if not cfg.configured:
        if required:
            raise RedisNotConfiguredError(
                "Redis 접속 정보가 없습니다 "
                "(REDIS_SENTINEL_HOST+REDIS_SENTINEL_MASTER, REDIS_URL, 또는 REDIS_HOST)"
            )
        return None

    import redis

    kwargs = cfg.client_kwargs()
    if cfg.use_sentinel:
        from redis.sentinel import Sentinel

        sentinel_kwargs = {"socket_timeout": cfg.socket_connect_timeout}
        if cfg.sentinel_password:
            sentinel_kwargs["password"] = cfg.sentinel_password
        sentinel = Sentinel(list(cfg.sentinel_hosts), sentinel_kwargs=sentinel_kwargs, **kwargs)
        # ⚠ 마스터 하나만 쓴다. 읽기를 복제본으로 돌리면 방금 쓴 값을 못 읽는
        # 복제 지연이 그대로 버그가 된다 (예: seq 발급 직후 조회).
        return sentinel.master_for(cfg.sentinel_master, **kwargs)
    if cfg.url:
        return redis.Redis.from_url(cfg.url, **kwargs)
    return redis.Redis(host=cfg.host, port=cfg.port, **kwargs)


def create_async_redis(
    *,
    settings: Optional[RedisSettings] = None,
    required: bool = True,
    **overrides: Any,
):
    """비동기 Redis 클라이언트. 계약은 :func:`create_sync_redis` 와 같다."""
    cfg = _resolve(settings, overrides)
    if not cfg.configured:
        if required:
            raise RedisNotConfiguredError(
                "Redis 접속 정보가 없습니다 "
                "(REDIS_SENTINEL_HOST+REDIS_SENTINEL_MASTER, REDIS_URL, 또는 REDIS_HOST)"
            )
        return None

    import redis.asyncio as aioredis

    kwargs = cfg.client_kwargs()
    if cfg.use_sentinel:
        from redis.asyncio.sentinel import Sentinel

        sentinel_kwargs = {"socket_timeout": cfg.socket_connect_timeout}
        if cfg.sentinel_password:
            sentinel_kwargs["password"] = cfg.sentinel_password
        sentinel = Sentinel(list(cfg.sentinel_hosts), sentinel_kwargs=sentinel_kwargs, **kwargs)
        return sentinel.master_for(cfg.sentinel_master, **kwargs)
    if cfg.url:
        return aioredis.Redis.from_url(cfg.url, **kwargs)
    return aioredis.Redis(host=cfg.host, port=cfg.port, **kwargs)


def ping_sync(client) -> bool:
    """살아 있는지. 예외를 올리지 않는다 — 헬스체크가 호출자를 죽이면 안 된다."""
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception as e:  # noqa: BLE001
        logger.warning("Redis ping 실패: %s", e)
        return False


async def ping_async(client) -> bool:
    """살아 있는지 (async). 예외를 올리지 않는다."""
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception as e:  # noqa: BLE001
        logger.warning("Redis ping 실패: %s", e)
        return False
