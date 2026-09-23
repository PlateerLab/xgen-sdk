"""설정 조회는 version 이 그대로인 동안 Redis 에 가지 않는다 (2.7.0).

2026-09-23 안정성 감사 F8.

무엇이 있었나
-------------
위성 파드(workflow·documents)의 설정 조회는 **매번** Redis GET 이었다. 동기 호출이고
이벤트 루프 위에서도 불린다. 평소엔 1ms 미만이지만 Redis 가 느려지면 조회 하나가 소켓
타임아웃(5초, 재시도 포함 최대 10초)만큼 루프를 세우고, 요청 하나가 설정을 여러 번 읽으므로
그 정지가 곱해진다.

고친 뒤
-------
값을 이름별로 기억하고 글로벌 version sentinel 이 그대로인 동안만 믿는다. version 은 1초에
한 번, 한 스레드만 읽는다. 쓰기(set_config·delete_config)는 version 을 INCR 하고 자기 기억을
버린다.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from xgen_sdk.config import redis_config as RC
from xgen_sdk.config.redis_config import RedisConfigManager

_VERSION = "config:_meta:version"


class _Redis:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.gets: list = []
        self.alive = True
        self.version_delay = 0.0

    def _check(self):
        if not self.alive:
            raise ConnectionError("redis down")

    def ping(self):
        self._check()
        return True

    def get(self, key):
        self._check()
        self.gets.append(key)
        if key == _VERSION and self.version_delay:
            time.sleep(self.version_delay)
        return self.data.get(key)

    def set(self, key, value):
        self._check()
        self.data[key] = value

    def sadd(self, key, member):
        self._check()

    def incr(self, key):
        self._check()
        self.data[key] = str(int(self.data.get(key) or 0) + 1)
        return int(self.data[key])

    def delete(self, key):
        self._check()
        self.data.pop(key, None)

    def srem(self, key, member):
        pass


def _entry(value, env_name="MODEL_DEFAULT"):
    return json.dumps({"value": value, "type": "string", "category": "llm",
                       "path": "llm.model_default", "env_name": env_name})


def _manager(redis: _Redis) -> RedisConfigManager:
    mgr = RedisConfigManager.__new__(RedisConfigManager)
    mgr.config_prefix = "config"
    mgr.version_key = _VERSION
    mgr.redis_client = redis
    mgr._connection_available = True
    mgr._last_connect_attempt = time.monotonic()
    mgr._reconnect_cooldown = 999.0
    mgr.recovery_epoch = 0
    mgr._last_write_version = 0
    mgr.db_manager = None
    return mgr


@pytest.fixture
def redis():
    return _Redis({_VERSION: "7", "config:MODEL_DEFAULT": _entry("gpt-4o")})


def _value_gets(r: _Redis) -> int:
    return sum(1 for k in r.gets if k != _VERSION)


class TestItStopsGoingToRedis:
    def test_repeated_reads_cost_one_round_trip(self, redis):
        mgr = _manager(redis)
        for _ in range(200):
            assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        assert _value_gets(redis) == 1, "같은 version 인데 조회마다 Redis 에 갔다"
        assert redis.gets.count(_VERSION) <= 2

    def test_a_missing_setting_is_remembered_too(self, redis):
        mgr = _manager(redis)
        for _ in range(50):
            assert mgr.probe_value("NOT_SET")[0] == "missing"
        assert _value_gets(redis) == 1

    def test_errors_are_never_remembered(self, redis):
        mgr = _manager(redis)
        mgr.probe_value("MODEL_DEFAULT")        # version 을 한 번 읽어 둔다
        redis.data["config:BROKEN"] = "{not json"
        assert mgr.probe_value("BROKEN")[0] == "error"
        redis.data["config:BROKEN"] = _entry("fixed", "BROKEN")
        assert mgr.probe_value("BROKEN") == ("ok", "fixed", "redis"), \
            "못 읽은 결과를 기억해 두었다 — 고친 뒤에도 계속 error"


class TestItStillSeesChanges:
    def test_another_pods_write_is_seen_after_the_recheck(self, redis, monkeypatch):
        mgr = _manager(redis)
        assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        # 다른 파드가 썼다 — 값과 version 이 함께 바뀐다.
        redis.data["config:MODEL_DEFAULT"] = _entry("claude-sonnet-4-6")
        redis.data[_VERSION] = "8"
        monkeypatch.setattr(RC, "VERSION_RECHECK_S", 0.0)
        assert mgr.get_config_value("MODEL_DEFAULT") == "claude-sonnet-4-6"

    def test_the_recheck_interval_is_a_second(self):
        assert RC.VERSION_RECHECK_S == 1.0

    def test_this_pods_own_write_is_seen_at_once(self, redis):
        mgr = _manager(redis)
        assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        mgr.set_config("llm.model_default", "claude-sonnet-4-6", env_name="MODEL_DEFAULT")
        assert mgr.get_config_value("MODEL_DEFAULT") == "claude-sonnet-4-6"

    def test_a_delete_is_seen_at_once(self, redis):
        mgr = _manager(redis)
        assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        mgr.delete_config("MODEL_DEFAULT")
        assert mgr.probe_value("MODEL_DEFAULT")[0] == "missing"

    def test_whoever_reads_a_new_version_invalidates_the_values(self, redis):
        """core 의 PersistentConfig 는 version 이 오른 것을 보자마자 값을 다시 읽는다 —
        그때 옛 값을 받으면 새 version 에 옛 값이 묶여 **다음 쓰기까지** 틀린다."""
        mgr = _manager(redis)
        assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        redis.data["config:MODEL_DEFAULT"] = _entry("claude-sonnet-4-6")
        redis.data[_VERSION] = "8"
        assert mgr.get_config_version() == 8          # 누군가(core)가 drift 를 봤다
        assert mgr.get_config_value("MODEL_DEFAULT") == "claude-sonnet-4-6"

    def test_a_flushed_redis_is_not_trusted(self, redis):
        """재시작·flush 로 version 키가 사라지면 0 은 '그대로' 가 아니라 '모른다' 다."""
        mgr = _manager(redis)
        assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        redis.data.clear()
        redis.data["config:MODEL_DEFAULT"] = _entry("restored-later")
        mgr._value_cache()["checked_at"] = 0.0
        assert mgr.get_config_value("MODEL_DEFAULT") == "restored-later"

    def test_a_reconnect_drops_what_was_remembered(self, redis):
        mgr = _manager(redis)
        assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        redis.data["config:MODEL_DEFAULT"] = _entry("written-to-db-during-outage")
        mgr.recovery_epoch += 1
        assert mgr.get_config_value("MODEL_DEFAULT") == "written-to-db-during-outage"

    def test_values_expire_even_if_the_version_never_moves(self, redis, monkeypatch):
        mgr = _manager(redis)
        assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        redis.data["config:MODEL_DEFAULT"] = _entry("written-without-incr")
        monkeypatch.setattr(RC, "VALUE_CACHE_MAX_AGE_S", 0.0001)
        time.sleep(0.001)
        assert mgr.get_config_value("MODEL_DEFAULT") == "written-without-incr"


class TestItIsSafeToShare:
    def test_mutating_a_returned_list_does_not_poison_the_cache(self, redis):
        redis.data["config:ROLES"] = json.dumps({"value": ["a", "b"], "type": "list"})
        mgr = _manager(redis)
        first = mgr.get_config_value("ROLES")
        first.append("INJECTED")
        assert mgr.get_config_value("ROLES") == ["a", "b"]

    def test_only_one_thread_waits_on_a_slow_version_read(self, redis):
        """Redis 가 느려도 나머지 조회는 줄을 서지 않고 마지막으로 본 version 을 쓴다."""
        mgr = _manager(redis)
        assert mgr.get_config_value("MODEL_DEFAULT") == "gpt-4o"
        mgr._value_cache()["checked_at"] = 0.0
        redis.version_delay = 0.3
        results, took = [], []

        def _read():
            t = time.monotonic()
            results.append(mgr.get_config_value("MODEL_DEFAULT"))
            took.append(time.monotonic() - t)

        threads = [threading.Thread(target=_read) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
        assert results == ["gpt-4o"] * 8
        assert sum(1 for d in took if d >= 0.25) == 1, took
        assert redis.gets.count(_VERSION) == 2      # 처음 한 번 + 느린 한 번

    def test_it_can_be_turned_off(self, redis, monkeypatch):
        monkeypatch.setattr(RC, "VALUE_CACHE_MAX_AGE_S", 0.0)
        mgr = _manager(redis)
        for _ in range(5):
            mgr.get_config_value("MODEL_DEFAULT")
        assert _value_gets(redis) == 5
