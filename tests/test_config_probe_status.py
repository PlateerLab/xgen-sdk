"""config 조회의 3-상태 API (1.39.0) — "못 읽었다" 를 숨기지 않는다.

기존 편의 API 의 문제:
  - ``RedisConfigManager.get_config_value`` 는 **연결 실패와 키 부재를 똑같이 default**
    로 돌려준다.
  - ``get_config_version`` 은 장애도 0 으로 돌려준다("버전 없음" 과 구분 불가).
  - ``ConfigComposer.refresh_all_if_stale`` 는 예외를 삼키고 False("갱신 안 함")를
    돌려준다 — "확인했는데 최신" 과 "확인조차 못 함" 이 같은 값이 된다.

그 결과 이 값들로 **보호 여부를 정하는 호출자**(권한 게이트·승인 게이트)에서 저장소
장애가 "설정 안 함" 으로 둔갑해 보호가 통째로 풀렸다(fail-open). 아래 probe_* /
refresh_all_if_stale_status 는 그 구분을 복원한다.
"""
from __future__ import annotations

import json

import pytest

from xgen_sdk.config.config_composer import ConfigComposer
from xgen_sdk.config.redis_config import RedisConfigManager


class _FakeRedis:
    def __init__(self, data=None, boom=False):
        self.data = data or {}
        self.boom = boom
        self.gets = 0

    def get(self, key):
        self.gets += 1
        if self.boom:
            raise ConnectionError("redis down")
        return self.data.get(key)


def _manager(data=None, boom=False, available=True):
    mgr = RedisConfigManager.__new__(RedisConfigManager)
    mgr.redis_client = _FakeRedis(data, boom)
    mgr._connection_available = available
    mgr.config_prefix = "config"
    mgr.version_key = "config:__version__"
    return mgr


def test_probe_value_distinguishes_ok_missing_error():
    payload = json.dumps({"value": True})
    mgr = _manager({"config:SWITCH": payload})
    assert mgr.probe_config_value("SWITCH") == ("ok", True)
    assert mgr.probe_config_value("OTHER") == ("missing", None)

    down = _manager(boom=True)
    assert down.probe_config_value("SWITCH") == ("error", None)

    unavailable = _manager(available=False)
    assert unavailable.probe_config_value("SWITCH") == ("error", None)
    assert unavailable.redis_client.gets == 0, "연결이 없으면 조회조차 하지 않는다"


def test_probe_value_never_masks_failure_as_default():
    """get_config_value 와의 대비 — 같은 장애에서 하나는 default, 하나는 error."""
    down = _manager(boom=True)
    assert down.get_config_value("SWITCH", "DEFAULT") == "DEFAULT"   # 기존 동작(마스킹)
    assert down.probe_config_value("SWITCH")[0] == "error"           # 새 동작(노출)


def test_probe_version_distinguishes_zero_from_failure():
    ok = _manager({"config:__version__": b"7"})
    assert ok.probe_config_version() == ("ok", 7)
    # 키가 없다 = 확인된 사실(0)
    assert _manager().probe_config_version() == ("ok", 0)
    # 장애 = 모른다
    assert _manager(boom=True).probe_config_version()[0] == "error"
    assert _manager(available=False).probe_config_version()[0] == "error"
    # 기존 API 는 셋을 구분하지 못한다
    assert _manager(boom=True).get_config_version() == 0
    assert _manager().get_config_version() == 0


class _Composer(ConfigComposer):
    """__init__ 을 건너뛰고 필요한 필드만 채운 테스트용 composer."""

    def __init__(self, manager, configs=None, last_version=0):
        self.redis_manager = manager
        self.config_categories = configs or {}
        self.all_configs = {}
        self._last_known_version = last_version
        import logging
        self.logger = logging.getLogger("test-composer")
        self.refreshed = 0

    def refresh_all(self):
        self.refreshed += 1


class _Category:
    def __init__(self, configs):
        self.configs = configs


class _Cfg:
    def __init__(self, value):
        self.value = value


def test_refresh_status_reports_unknown_when_it_cannot_verify():
    down = _Composer(_manager(boom=True))
    assert down.refresh_all_if_stale_status() == ConfigComposer.REFRESH_UNKNOWN
    assert down.refreshed == 0
    # 구 API 는 같은 상황에서 False("갱신 안 함") 라 최신인 것과 구분되지 않는다.
    assert down.refresh_all_if_stale() is False


def test_refresh_status_reports_fresh_and_refreshed():
    fresh = _Composer(_manager({"config:__version__": b"0"}), last_version=0)
    assert fresh.refresh_all_if_stale_status() == ConfigComposer.REFRESH_FRESH
    assert fresh.refreshed == 0

    drifted = _Composer(_manager({"config:__version__": b"9"}), last_version=3)
    assert drifted.refresh_all_if_stale_status() == ConfigComposer.REFRESH_REFRESHED
    assert drifted.refreshed == 1


def test_composer_probe_refuses_stale_cache_when_freshness_is_unverifiable():
    """핵심: 최신성을 확인 못 하면 **캐시 값을 확정 답변으로 내주지 않는다**.

    이것이 없으면 "캐시=false(낡음) + Redis 장애 + DB=true" 에서 false 가 확정 답변이
    되어 보호가 풀린다(호출자가 DB 폴백을 탈 기회조차 잃는다).
    """
    cached_false = {"app": _Category({"SWITCH": _Cfg(False)})}
    down = _Composer(_manager(boom=True), configs=cached_false)
    assert down.probe_config_value("SWITCH") == ("error", None)

    ok = _Composer(_manager({"config:__version__": b"0"}), configs=cached_false, last_version=0)
    assert ok.probe_config_value("SWITCH") == ("ok", False)
    # 최신성 확인을 건너뛰라고 명시하면 캐시 값을 준다(관찰/디버깅용).
    assert down.probe_config_value("SWITCH", verify_freshness=False) == ("ok", False)


def test_composer_probe_missing_when_key_is_not_declared():
    ok = _Composer(_manager({"config:__version__": b"0"}), configs={"app": _Category({})})
    assert ok.probe_config_value("SWITCH") == ("missing", None)
