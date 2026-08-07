"""Redis 접속의 단일 진입점 — 세 접속 방식과 그 경계를 고정한다.

왜 이 모듈이 생겼나 (2026-08-06 감사): ``service/redis_sentinel.py`` 가
xgen-core · xgen-workflow · xgen-documents 세 저장소에 **복사본**으로 있었고
도입 7일 만에 갈라졌다(core 사본에는 async 변형이 없고, socket timeout 이
2초 대 5초). 게다가 SDK 자신은 Sentinel 을 몰라서, 페일오버 뒤 서비스는 새
마스터를 따라가는데 SDK 만 강등된 옛 노드에 붙어 쓰기가 실패했다.
"""

import unittest
from unittest.mock import MagicMock, patch

from xgen_sdk.redis import (
    RedisNotConfiguredError,
    RedisSettings,
    create_async_redis,
    create_sync_redis,
    ping_sync,
)


class SettingsFromEnvTestCase(unittest.TestCase):
    """배포된 환경변수 이름을 그대로 읽는다 — **인프라를 바꾸지 않는다.**"""

    def test_sentinel_needs_both_hosts_and_master(self):
        # 하나만 있으면 설정 실수다. 조용히 아무 데도 못 붙느니 직결로 간다.
        only_hosts = RedisSettings.from_env({"REDIS_SENTINEL_HOST": "a", "REDIS_HOST": "r"})
        self.assertFalse(only_hosts.use_sentinel)
        only_master = RedisSettings.from_env({"REDIS_SENTINEL_MASTER": "m", "REDIS_HOST": "r"})
        self.assertFalse(only_master.use_sentinel)
        both = RedisSettings.from_env(
            {"REDIS_SENTINEL_HOST": "a", "REDIS_SENTINEL_MASTER": "m"}
        )
        self.assertTrue(both.use_sentinel)

    def test_sentinel_host_list_parsing(self):
        s = RedisSettings.from_env(
            {
                "REDIS_SENTINEL_HOST": "a:26379, b , c:26380",
                "REDIS_SENTINEL_MASTER": "m",
                "REDIS_SENTINEL_PORT": "26400",
            }
        )
        self.assertEqual(
            s.sentinel_hosts, (("a", 26379), ("b", 26400), ("c", 26380)),
            "포트 없는 항목은 REDIS_SENTINEL_PORT 를 써야 한다",
        )

    def test_gateway_uses_the_same_env_names(self):
        """Rust 게이트웨이가 읽는 이름과 어긋나면 한쪽만 Sentinel 을 탄다."""
        from xgen_sdk.redis import SENTINEL_ENV

        self.assertEqual(
            set(SENTINEL_ENV),
            {"REDIS_SENTINEL_HOST", "REDIS_SENTINEL_PORT", "REDIS_SENTINEL_MASTER"},
        )

    def test_missing_config_is_not_guessed(self):
        """예전 SDK 는 host 기본값이 사내 IP, password 가 실제 비밀번호였다.
        설정이 빠지면 엉뚱한 서버에 붙느니 **안 붙는** 편이 낫다."""
        s = RedisSettings.from_env({})
        self.assertIsNone(s.host)
        self.assertIsNone(s.password)
        self.assertFalse(s.configured)

    def test_url_takes_precedence_over_host(self):
        s = RedisSettings.from_env({"REDIS_URL": "redis://u:p@h:6380/1", "REDIS_HOST": "other"})
        self.assertTrue(s.configured)
        self.assertIn("h:6380", s.describe())

    def test_describe_never_leaks_the_password(self):
        s = RedisSettings.from_env(
            {"REDIS_HOST": "h", "REDIS_PASSWORD": "s3cr3t", "REDIS_URL": "redis://u:s3cr3t@h:6379"}
        )
        self.assertNotIn("s3cr3t", s.describe())

    def test_timeouts_come_from_env_but_can_be_overridden(self):
        s = RedisSettings.from_env({"REDIS_HOST": "h", "REDIS_SOCKET_TIMEOUT": "12"})
        self.assertEqual(s.socket_timeout, 12.0)
        # None 은 '무한 대기' 라는 **의미 있는 값**이다 (블로킹 구독).
        self.assertIsNone(s.with_overrides(socket_timeout=None).socket_timeout)

    def test_overrides_do_not_mutate_the_original(self):
        base = RedisSettings.from_env({"REDIS_HOST": "h"})
        derived = base.with_overrides(db=7)
        self.assertEqual(base.db, 0)
        self.assertEqual(derived.db, 7)


class SyncClientTestCase(unittest.TestCase):
    def test_direct_connection(self):
        s = RedisSettings(host="h", port=6380, password="pw", db=3, decode_responses=True)
        with patch("redis.Redis") as R:
            create_sync_redis(settings=s)
        _, kwargs = R.call_args
        self.assertEqual(kwargs["db"], 3)
        self.assertTrue(kwargs["decode_responses"])
        self.assertEqual(kwargs["password"], "pw")

    def test_url_connection(self):
        s = RedisSettings(url="redis://h:6379/0", db=2)
        with patch("redis.Redis") as R:
            create_sync_redis(settings=s)
        R.from_url.assert_called_once()
        self.assertEqual(R.from_url.call_args[0][0], "redis://h:6379/0")

    def test_sentinel_uses_the_master_for_reads_and_writes(self):
        """복제본으로 읽으면 방금 쓴 값을 못 읽는 복제 지연이 그대로 버그가 된다
        (예: seq 발급 직후 조회)."""
        s = RedisSettings(
            sentinel_hosts=(("a", 26379),), sentinel_master="mymaster", password="pw", db=1
        )
        with patch("redis.sentinel.Sentinel") as S:
            create_sync_redis(settings=s)
        S.assert_called_once()
        self.assertEqual(S.call_args[0][0], [("a", 26379)])
        S.return_value.master_for.assert_called_once()
        self.assertEqual(S.return_value.master_for.call_args[0][0], "mymaster")
        # slave_for 는 절대 쓰지 않는다
        self.assertFalse(S.return_value.slave_for.called)

    def test_sentinel_password_is_passed_to_the_sentinel_nodes(self):
        s = RedisSettings(
            sentinel_hosts=(("a", 26379),),
            sentinel_master="m",
            password="datapw",
            sentinel_password="sentinelpw",
        )
        with patch("redis.sentinel.Sentinel") as S:
            create_sync_redis(settings=s)
        self.assertEqual(S.call_args.kwargs["sentinel_kwargs"]["password"], "sentinelpw")

    def test_unconfigured_raises_by_default(self):
        with self.assertRaises(RedisNotConfiguredError):
            create_sync_redis(settings=RedisSettings())

    def test_unconfigured_returns_none_when_optional(self):
        """Redis 가 선택 기능인 호출처(메모리 모드 폴백)를 위한 계약."""
        self.assertIsNone(create_sync_redis(settings=RedisSettings(), required=False))

    def test_overrides_apply_on_top_of_settings(self):
        s = RedisSettings(host="h", db=0)
        with patch("redis.Redis") as R:
            create_sync_redis(settings=s, db=9, decode_responses=True)
        self.assertEqual(R.call_args.kwargs["db"], 9)

    def test_none_timeout_survives_the_override_path(self):
        """구독자가 무한 대기를 고르는 경우 — None 이 '미지정' 으로 먹히면 안 된다."""
        with patch("redis.Redis") as R:
            create_sync_redis(settings=RedisSettings(host="h"), socket_timeout=None)
        self.assertIsNone(R.call_args.kwargs["socket_timeout"])

    def test_stale_sockets_are_health_checked(self):
        """페일오버·컨테이너 재시작 뒤 죽은 소켓을 붙들고 있으면 안 된다."""
        with patch("redis.Redis") as R:
            create_sync_redis(settings=RedisSettings(host="h"))
        self.assertGreater(R.call_args.kwargs["health_check_interval"], 0)


class AsyncClientTestCase(unittest.TestCase):
    """core 사본에는 async 변형이 아예 없었다 — 그래서 통일이 필요했다."""

    def test_async_direct(self):
        with patch("redis.asyncio.Redis") as R:
            create_async_redis(settings=RedisSettings(host="h", db=4))
        self.assertEqual(R.call_args.kwargs["db"], 4)

    def test_async_sentinel(self):
        s = RedisSettings(sentinel_hosts=(("a", 26379),), sentinel_master="m")
        with patch("redis.asyncio.sentinel.Sentinel") as S:
            create_async_redis(settings=s)
        S.return_value.master_for.assert_called_once_with("m", **S.return_value.master_for.call_args.kwargs)

    def test_async_unconfigured_optional(self):
        self.assertIsNone(create_async_redis(settings=RedisSettings(), required=False))


class PingTestCase(unittest.TestCase):
    def test_ping_never_raises(self):
        """헬스체크가 호출자를 죽이면 안 된다."""
        bad = MagicMock()
        bad.ping.side_effect = RuntimeError("down")
        self.assertFalse(ping_sync(bad))
        self.assertFalse(ping_sync(None))
        ok = MagicMock()
        ok.ping.return_value = True
        self.assertTrue(ping_sync(ok))


if __name__ == "__main__":
    unittest.main()


class ConfigManagerFollowsSentinelTestCase(unittest.TestCase):
    """config 계층이 **서비스와 같은 노드**를 봐야 한다.

    예전에는 RedisConfigManager 가 직접 ``redis.Redis(host=…)`` 를 불렀다.
    그래서 서비스의 클라이언트는 Sentinel 로 새 마스터를 따라가는데 config 만
    고정 주소에 남았고, 페일오버 뒤 설정 쓰기가 read-only 오류로 실패했다.
    """

    def test_manager_uses_the_shared_factory(self):
        src = open("src/xgen_sdk/config/redis_config.py", encoding="utf-8").read()
        self.assertIn("create_sync_redis(settings=cfg)", src)
        self.assertNotIn(
            "self.redis_client = redis.Redis(",
            src,
            "config 계층이 자기 손으로 클라이언트를 만든다 — Sentinel 을 못 따라간다",
        )

    def test_no_hardcoded_host_or_password_defaults(self):
        """설정이 빠지면 조용히 엉뚱한 서버에 붙는 것보다 안 붙는 편이 낫다."""
        src = open("src/xgen_sdk/config/redis_config.py", encoding="utf-8").read()
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.strip().startswith("#") and "⚠" not in ln
        )
        self.assertNotIn("192.168.2.242", code)
        self.assertNotIn("redis_secure_password123", code)

    def test_sentinel_env_reaches_the_manager(self):
        s = RedisSettings.from_env(
            {"REDIS_SENTINEL_HOST": "s1:26379", "REDIS_SENTINEL_MASTER": "mymaster"}
        )
        self.assertTrue(s.use_sentinel, "config 도 Sentinel 설정을 그대로 읽어야 한다")


class ExistingPublicApiMustSurviveTestCase(unittest.TestCase):
    """``xgen_sdk.redis`` 는 **이미 쓰이던 패키지**다 — 새 팩토리를 넣으면서
    기존 export 를 지우면 10곳 넘는 호출처가 즉시 ImportError 로 죽는다
    (LLM/오디오 카탈로그 캐시, 실행 세션 관리 등).
    """

    def test_singleton_client_is_still_exported(self):
        import xgen_sdk.redis as pkg

        for name in ("RedisClient", "get_redis_client"):
            self.assertTrue(hasattr(pkg, name), f"기존 공개 API 가 사라졌다: {name}")
            self.assertIn(name, pkg.__all__)

    def test_submodule_import_path_still_works(self):
        """호출처 다수가 `from xgen_sdk.redis.client import get_redis_client` 형태다."""
        from xgen_sdk.redis.client import RedisClient, get_redis_client  # noqa: F401

    def test_singleton_client_goes_through_the_factory(self):
        """가장 넓게 쓰이는 경로 — 여기가 Sentinel 을 모르면 페일오버에 취약하다."""
        src = open("src/xgen_sdk/redis/client.py", encoding="utf-8").read()
        self.assertIn("create_sync_redis(settings=self._settings)", src)
        self.assertNotIn(
            "self._redis_client = redis.Redis(",
            src,
            "싱글턴 클라이언트가 자기 손으로 접속을 만든다",
        )

    def test_no_hardcoded_password_default(self):
        src = open("src/xgen_sdk/redis/client.py", encoding="utf-8").read()
        self.assertNotIn("redis_secure_password123", src)
