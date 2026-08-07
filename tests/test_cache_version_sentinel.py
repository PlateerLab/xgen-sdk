"""범용 캐시 version sentinel — Pod 간 캐시 무효화의 표준 수단.

왜 SDK 에 있나: config 말고도 **Pod 별 in-memory 캐시**를 두는 곳이 여럿이다
(예: 사용자 preferences 게이트). TTL 로만 버티면 "쓰기가 반영되기까지 최대
TTL" 이라는 staleness 를 늘 지고 간다. 쓰기 쪽이 INCR 하고 읽기 쪽이 값만
비교하면 그 staleness 가 사라진다 — config 가 이미 쓰는 방식이고, 서비스마다
각자 Redis 를 만지지 않도록 SDK 가 표준으로 제공한다.
"""

import unittest
from unittest.mock import MagicMock

from xgen_sdk.config.redis_config import RedisConfigManager
from xgen_sdk.config.local_config import LocalConfigManager


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.fail = False

    def incr(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def get(self, key):
        if self.fail:
            raise RuntimeError("redis down")
        v = self.store.get(key)
        return None if v is None else str(v)


def _redis_manager() -> RedisConfigManager:
    m = RedisConfigManager.__new__(RedisConfigManager)
    m.redis_client = _FakeRedis()
    m._connection_available = True
    m.config_prefix = "config"
    m.version_key = "config:_meta:version"
    m._last_write_version = 0
    m.db_manager = None
    return m


class RedisSentinelTestCase(unittest.TestCase):
    def test_bump_moves_the_version_and_readers_see_it(self):
        m = _redis_manager()
        self.assertEqual(m.get_cache_version("user_prefs"), 0, "키가 없으면 0")
        v1 = m.bump_cache_version("user_prefs")
        self.assertEqual(m.get_cache_version("user_prefs"), v1)
        v2 = m.bump_cache_version("user_prefs")
        self.assertGreater(v2, v1, "버전이 전진하지 않으면 무효화가 안 된다")

    def test_namespaces_do_not_bleed(self):
        """한 도메인의 쓰기가 다른 도메인의 캐시를 무효화하면 무관한 재조회가 연쇄된다."""
        m = _redis_manager()
        m.bump_cache_version("user_prefs")
        self.assertEqual(m.get_cache_version("other"), 0, "네임스페이스가 샜다")

    def test_never_collides_with_the_config_sentinel(self):
        m = _redis_manager()
        m.bump_cache_version("user_prefs")
        self.assertNotIn(m.version_key, m.redis_client.store, "config sentinel 을 건드렸다")

    def test_redis_outage_reports_zero_not_a_wrong_version(self):
        """0 = '버전 정보 없음' — 읽기 쪽이 TTL 로 폴백하는 신호다.
        여기서 낡은 값을 지어내면 Redis 장애가 곧 stale 고착이 된다."""
        m = _redis_manager()
        m.bump_cache_version("user_prefs")
        m.redis_client.fail = True
        self.assertEqual(m.get_cache_version("user_prefs"), 0)

    def test_bump_failure_never_raises(self):
        """캐시 무효화가 사용자의 쓰기 자체를 실패시키면 안 된다."""
        m = _redis_manager()
        m.redis_client.fail = True
        self.assertEqual(m.bump_cache_version("user_prefs"), 0)

    def test_disconnected_manager_is_quiet(self):
        m = _redis_manager()
        m._connection_available = False
        self.assertEqual(m.get_cache_version("x"), 0)
        self.assertEqual(m.bump_cache_version("x"), 0)


class LocalSentinelTestCase(unittest.TestCase):
    """Redis 가 없는 배포에서도 계약이 성립해야 한다 (단일 파드 의미론)."""

    def test_local_counter_advances(self):
        m = LocalConfigManager.__new__(LocalConfigManager)
        m._cache_versions = {}
        first = m.get_cache_version("user_prefs")
        self.assertGreater(first, 0, "0 은 '정보 없음' 신호라 여기선 쓰면 안 된다")
        self.assertGreater(m.bump_cache_version("user_prefs"), first)

    def test_local_namespaces_are_separate(self):
        m = LocalConfigManager.__new__(LocalConfigManager)
        m._cache_versions = {}
        m.bump_cache_version("a")
        self.assertNotEqual(m.get_cache_version("a"), m.get_cache_version("b"))


class PassthroughTestCase(unittest.TestCase):
    """서비스는 `_manager` 같은 내부를 만지지 않는다 — 상위 표면으로 부른다."""

    def test_config_client_passes_through(self):
        from xgen_sdk.config.config_client import ConfigClient

        c = ConfigClient.__new__(ConfigClient)
        c._manager = MagicMock()
        c._manager.get_cache_version.return_value = 7
        c._manager.bump_cache_version.return_value = 8
        self.assertEqual(c.get_cache_version("user_prefs"), 7)
        self.assertEqual(c.bump_cache_version("user_prefs"), 8)

    def test_passthrough_swallows_manager_errors(self):
        from xgen_sdk.config.config_client import ConfigClient

        c = ConfigClient.__new__(ConfigClient)
        c._manager = MagicMock()
        c._manager.get_cache_version.side_effect = RuntimeError("boom")
        self.assertEqual(c.get_cache_version("x"), 0, "캐시 힌트가 요청을 실패시켰다")


if __name__ == "__main__":
    unittest.main()
