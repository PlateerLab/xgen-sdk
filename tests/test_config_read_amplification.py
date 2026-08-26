"""Config 조회 증폭 — 요청 1건에 Redis GET 수백 회가 찍히던 문제의 회귀 가드.

관측(2026-08-24): `/api/agentflow/execute/{id}/stream` 트레이스 1건에 GET span
488~489회. 워크플로우 내용과 무관하게 매번 같은 횟수였고 캐싱 효과도 없었다.

원인은 `get_config_by_name()` 의 **미스 폴백**이었다. env_name 으로 값을 못 찾으면
(키가 없거나 값이 None) 전체 config 를 스윕했는데, 그 스윕이
`KEYS config:*` + **키마다 GET 1회**였다. config 240여 개 환경에서 조회 1번이
240여 회의 왕복이 된다. xgen-workflow 는 이 조회를 81곳에서 쓴다.

여기서 고정하는 계약:
  1. 전체 스윕은 KEYS 가 아니라 SCAN 을 쓴다 (KEYS 는 Redis 를 블로킹한다).
  2. 값 조회는 키마다 GET 이 아니라 MGET 배치를 쓴다.
  3. 미스 폴백 결과는 글로벌 version 으로 가드해 캐시한다 — 같은 이름을 반복
     조회해도 스윕은 version 당 1회.
  4. 값이 바뀌면(version INCR) 다음 조회에서 인덱스가 재구성된다.
"""

import json
import unittest

from xgen_sdk.config.redis_config import RedisConfigManager


class _CountingRedis:
    """호출 횟수를 세는 최소 Redis 더블."""

    def __init__(self, configs: dict, version: int = 1):
        # configs: env_name -> {"value": ..., "path": ..., "env_name": ...}
        self.store = {f"config:{k}": json.dumps(v) for k, v in configs.items()}
        self.store["config:_meta:version"] = str(version)
        self.store["config:category:app"] = "index-set-not-json"
        self.calls = {"get": 0, "mget": 0, "scan_iter": 0, "keys": 0}

    def get(self, key):
        self.calls["get"] += 1
        return self.store.get(key)

    def mget(self, keys):
        self.calls["mget"] += 1
        return [self.store.get(k) for k in keys]

    def scan_iter(self, match=None, count=None):
        self.calls["scan_iter"] += 1
        return list(self.store.keys())

    def keys(self, pattern):  # 쓰이면 계약 위반
        self.calls["keys"] += 1
        return list(self.store.keys())

    def incr(self, key):
        cur = int(self.store.get(key, 0)) + 1
        self.store[key] = str(cur)
        return cur


def _manager(redis) -> RedisConfigManager:
    m = RedisConfigManager.__new__(RedisConfigManager)
    m.redis_client = redis
    m._connection_available = True
    m.config_prefix = "config"
    m.version_key = "config:_meta:version"
    m._last_write_version = 0
    m.db_manager = None
    m._name_index_cache = None
    m._name_index_tail_cache = None
    m._name_index_version = -1
    return m


def _configs(n: int) -> dict:
    return {
        f"CFG_{i}": {"value": f"v{i}", "path": f"app.cfg_{i}", "env_name": f"CFG_{i}"}
        for i in range(n)
    }


class ConfigReadAmplificationTestCase(unittest.TestCase):
    def test_get_all_configs_uses_scan_and_mget_not_per_key_get(self):
        """스윕 1회의 비용이 키 개수에 비례한 GET 왕복이면 안 된다."""
        redis = _CountingRedis(_configs(240))
        m = _manager(redis)

        configs = m.get_all_configs()

        self.assertEqual(len(configs), 240, "모든 config 를 읽어야 한다")
        self.assertEqual(redis.calls["keys"], 0, "KEYS 는 Redis 를 블로킹한다 — 금지")
        self.assertEqual(redis.calls["get"], 0, "키마다 GET 왕복이 증폭의 원인이었다")
        self.assertGreaterEqual(redis.calls["scan_iter"], 1)
        self.assertLessEqual(redis.calls["mget"], 2, "240개는 배치 1~2회로 끝나야 한다")

    def test_missing_config_lookup_sweeps_only_once_per_version(self):
        """없는 이름을 반복 조회해도 스윕은 version 당 1회."""
        redis = _CountingRedis(_configs(240))
        m = _manager(redis)

        for _ in range(5):
            with self.assertRaises(KeyError):
                m.get_config_by_name("NOT_THERE")

        self.assertEqual(redis.calls["scan_iter"], 1, "스윕은 1회여야 한다")
        # 조회마다 env_name GET 1회 + version GET 1회 = 조회당 2회.
        # 예전 구현은 여기에 키 개수(240)×조회수(5) 가 더 붙었다.
        self.assertLessEqual(
            redis.calls["get"], 12,
            f"조회당 상수 회 왕복이어야 한다 (실제 {redis.calls['get']}회)",
        )

    def test_lookup_by_path_and_tail_still_works(self):
        """폴백의 의미(경로/끝조각 매칭)는 그대로 유지된다."""
        redis = _CountingRedis({
            "REAL_NAME": {"value": "hit", "path": "llm.default_model", "env_name": "REAL_NAME"},
        })
        m = _manager(redis)

        self.assertEqual(m.get_config_by_name("REAL_NAME"), "hit", "env_name 직접 조회")
        self.assertEqual(m.get_config_by_name("llm.default_model"), "hit", "전체 path")
        self.assertEqual(m.get_config_by_name("default_model"), "hit", "path 끝조각")

    def test_version_bump_rebuilds_the_index(self):
        """값이 바뀌면(version INCR) 다음 조회는 새 값을 본다 — stale 금지."""
        redis = _CountingRedis({
            "A": {"value": "old", "path": "app.a", "env_name": "A"},
        })
        m = _manager(redis)

        self.assertEqual(m.get_config_by_name("a"), "old", "끝조각 조회로 인덱스 적재")

        redis.store["config:A"] = json.dumps({"value": "new", "path": "app.a", "env_name": "A"})
        redis.incr("config:_meta:version")  # write 는 항상 version 을 올린다

        self.assertEqual(m.get_config_by_name("a"), "new", "version 이 바뀌면 재구성")
        self.assertEqual(redis.calls["scan_iter"], 2, "재구성은 정확히 1회 더")

    def test_sweep_skips_category_and_meta_keys(self):
        """인덱스 셋(config:category:*)·메타 키는 config 로 오인되면 안 된다."""
        redis = _CountingRedis(_configs(3))
        m = _manager(redis)

        configs = m.get_all_configs()

        self.assertEqual(len(configs), 3)
        self.assertTrue(all("value" in c for c in configs))


class SensitiveConfigLoggingTestCase(unittest.TestCase):
    """config 로더가 값을 INFO 로그에 평문으로 남기던 문제."""

    def test_secret_like_names_are_masked(self):
        from xgen_sdk.config.base_config import _mask_config_value

        for name in ("OPENAI_API_KEY", "DB_PASSWORD", "JWT_SECRET", "AWS_SESSION_TOKEN"):
            masked = str(_mask_config_value(name, "super-secret-value"))
            self.assertNotIn("super-secret-value", masked, f"{name} 값이 새면 안 된다")
            self.assertIn("redacted", masked)

    def test_presence_is_still_observable(self):
        """진단을 위해 '값이 비었는지' 는 남아야 한다."""
        from xgen_sdk.config.base_config import _mask_config_value

        self.assertEqual(_mask_config_value("API_KEY", None), "<unset>")
        self.assertEqual(_mask_config_value("API_KEY", ""), "<empty>")

    def test_non_sensitive_values_are_untouched(self):
        from xgen_sdk.config.base_config import _mask_config_value

        self.assertEqual(_mask_config_value("WORKFLOW_MAX_WORKERS", 8), 8)
        self.assertEqual(_mask_config_value("DEFAULT_LLM_PROVIDER", "openai"), "openai")


if __name__ == "__main__":
    unittest.main()
