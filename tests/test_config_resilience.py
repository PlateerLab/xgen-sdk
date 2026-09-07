"""config 계층 복원력 (1.40.0) — 우리 시스템의 철학을 코드로 고정한다.

철학:
  1. **Redis 가 죽어도 서비스는 돈다.** 설정 값은 DB(persistent_configs)에도 있고
     PersistentConfig 가 DB 를 먼저 본다. 그러니 Redis 부재는 '느려지지 않는 degrade' 여야 한다.
  2. **Redis 가 살아나면 반드시 다시 Redis 로 돌아온다.** 각 프로세스가 스스로 재접속을
     시도하고, 돌아온 순간 캐시를 재동기화한다.
  3. **DB 가 죽으면 서비스는 제대로 동작하지 않는다** — 그건 정상이다. 그래서 Redis·DB 가
     동시에 죽은 상태를 '보안 노출' 로 다룰 필요가 없다(읽을 것도, 내줄 것도 없다).
     대신 DB 계층은 계속 복구를 시도한다(pool_manager.with_retry / health_check(auto_recover)).

이 파일은 1·2 를 지킨다. 예전 구현의 실제 결함:
  - RedisConfigManager 는 __init__ 에서 **한 번만** 붙었다 → 한 번 끊기면 그 프로세스는
    영원히 Redis 를 쓰지 못했다.
  - create_config_manager 는 Redis 실패 시 **None** 을 돌려줬다 → composer.redis_manager 가
    None 이 되어 호출부가 깨지고, 복구 경로도 사라졌다.
"""
from __future__ import annotations

import json

import pytest

from xgen_sdk.config.config_composer import ConfigComposer
from xgen_sdk.config.local_config import LocalConfigManager, create_config_manager
from xgen_sdk.config.redis_config import RedisConfigManager


class _FlakyRedis:
    """지정한 시점부터 살아나는 가짜 Redis."""

    def __init__(self, alive=False, data=None):
        self.alive = alive
        self.data = data or {}
        self.pings = 0

    def ping(self):
        self.pings += 1
        if not self.alive:
            raise ConnectionError("redis down")
        return True

    def get(self, key):
        if not self.alive:
            raise ConnectionError("redis down")
        return self.data.get(key)


def _manager(alive=False, data=None, cooldown=0.0):
    """__init__ 을 우회해 재연결 로직만 시험한다."""
    mgr = RedisConfigManager.__new__(RedisConfigManager)
    mgr._settings = None
    mgr._host, mgr._port = "h", 6379
    mgr.config_prefix = "config"
    mgr.version_key = "config:__version__"
    mgr.redis_client = _FlakyRedis(alive=alive, data=data)
    mgr._connection_available = alive
    mgr._last_connect_attempt = 0.0
    mgr._reconnect_cooldown = cooldown
    mgr.recovery_epoch = 0
    return mgr


def _patch_connect(mgr, monkeypatch):
    """_connect 를 가짜 클라이언트 상태에 맞춰 동작하게 바꾼다(팩토리 우회)."""
    def _connect(initial=False):
        import time as _t
        mgr._last_connect_attempt = _t.monotonic()
        try:
            mgr.redis_client.ping()
        except Exception:
            mgr._connection_available = False
            return False
        was_down = not mgr._connection_available
        mgr._connection_available = True
        if was_down and not initial:
            mgr.recovery_epoch += 1
        return True
    monkeypatch.setattr(mgr, "_connect", _connect)


def test_dead_redis_does_not_block_reads(monkeypatch):
    """죽어 있으면 조회는 error 로 즉시 답한다 — 매 요청 접속 타임아웃을 물지 않는다."""
    mgr = _manager(alive=False, cooldown=60.0)
    _patch_connect(mgr, monkeypatch)
    import time as _t
    mgr._last_connect_attempt = _t.monotonic()   # 방금 시도해 실패한 상태
    assert mgr.probe_config_value("SWITCH") == ("error", None)
    assert mgr.redis_client.pings == 0, "쿨다운 안에서는 재접속을 반복하지 않는다"


def test_recovers_automatically_when_redis_comes_back(monkeypatch):
    """철학 2 — 살아나면 자동으로 다시 Redis 를 쓴다."""
    mgr = _manager(alive=False, cooldown=0.0, data={"config:SWITCH": json.dumps({"value": True})})
    _patch_connect(mgr, monkeypatch)
    assert mgr.probe_config_value("SWITCH")[0] == "error"

    mgr.redis_client.alive = True          # Redis 복구
    status, value = mgr.probe_config_value("SWITCH")
    assert (status, value) == ("ok", True), "복구되면 다음 조회부터 바로 Redis 를 쓴다"
    assert mgr.recovery_epoch == 1, "복구를 epoch 로 알린다(상위 계층이 재동기화 판단)"


def test_health_check_attempts_recovery(monkeypatch):
    mgr = _manager(alive=False, cooldown=0.0)
    _patch_connect(mgr, monkeypatch)
    assert mgr.health_check() is False
    mgr.redis_client.alive = True
    assert mgr.health_check() is True, "health_check 는 복구를 시도한다"
    assert mgr.health_check(auto_recover=False) is True


def test_factory_never_returns_none_when_db_exists():
    """철학 1 — Redis 가 없어도 매니저는 살아 있어야 한다(값은 DB 에서 읽는다)."""
    class _Down(RedisConfigManager):
        def __init__(self, db_manager=None):  # noqa: D107
            self._connection_available = False
            self.recovery_epoch = 0

    import xgen_sdk.config.local_config as lc
    original = lc.RedisConfigManager if hasattr(lc, "RedisConfigManager") else None
    import xgen_sdk.config.redis_config as rc
    saved = rc.RedisConfigManager
    rc.RedisConfigManager = _Down
    try:
        mgr = create_config_manager(db_manager=object())
        assert mgr is not None, "DB 가 있으면 None 을 돌려주면 안 된다"
        assert isinstance(mgr, _Down)
        # DB 도 없으면 메모리 매니저로라도 내려간다.
        mem = create_config_manager(db_manager=None)
        assert isinstance(mem, LocalConfigManager)
    finally:
        rc.RedisConfigManager = saved
        if original is not None:
            lc.RedisConfigManager = original


class _Composer(ConfigComposer):
    def __init__(self, manager):
        import logging
        self.redis_manager = manager
        self.config_categories = {}
        self.all_configs = {}
        self._last_known_version = 0
        self._last_recovery_epoch = int(getattr(manager, "recovery_epoch", 0) or 0)
        self.logger = logging.getLogger("test")
        self.refreshed = 0

    def refresh_all(self):
        self.refreshed += 1


def test_composer_resyncs_after_redis_recovery(monkeypatch):
    """복구 감지 시 전체 재동기화 — 그래야 Redis 가 다시 정본이 된다."""
    mgr = _manager(alive=True, data={"config:__version__": b"0"}, cooldown=0.0)
    _patch_connect(mgr, monkeypatch)
    composer = _Composer(mgr)
    assert composer.refresh_all_if_stale_status() == ConfigComposer.REFRESH_FRESH
    assert composer.refreshed == 0

    mgr.recovery_epoch += 1     # 재연결이 일어났다
    assert composer.refresh_all_if_stale_status() == ConfigComposer.REFRESH_REFRESHED
    assert composer.refreshed == 1, "재연결 뒤에는 캐시를 다시 맞춘다"

    # 같은 epoch 에서는 반복 refresh 하지 않는다.
    assert composer.refresh_all_if_stale_status() == ConfigComposer.REFRESH_FRESH
    assert composer.refreshed == 1


def test_probe_db_config_distinguishes_missing_from_error():
    """DB 쪽도 같은 원칙 — '행 없음' 과 '조회 실패' 를 구분한다."""
    from xgen_sdk.db.db_config_helper import probe_db_config

    class _Mgr:
        """_is_db_available 이 보는 풀 상태까지 갖춘 가짜 DB 매니저."""

        db_type = "postgresql"

        def __init__(self, result=None, boom=False, healthy=True):
            self.result, self.boom, self.healthy = result, boom, healthy

        def _is_pool_healthy(self):
            return self.healthy

        def execute_query_one(self, *a, **k):
            if self.boom:
                raise ConnectionError("db down")
            return self.result

    ok = _Mgr({"config_value": "true", "data_type": "bool"})   # SDK 규약: bool
    assert probe_db_config(ok, "app.switch") == ("ok", True)
    assert probe_db_config(_Mgr(None), "app.switch") == ("missing", None)
    assert probe_db_config(_Mgr(boom=True), "app.switch") == ("error", None)
    assert probe_db_config(None, "app.switch") == ("error", None)
    # 풀이 죽어 있으면 조회 자체를 시도하지 않고 error — "행 없음" 으로 오해하지 않는다.
    assert probe_db_config(_Mgr(healthy=False), "app.switch") == ("error", None)
