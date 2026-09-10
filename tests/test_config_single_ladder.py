"""설정을 읽는 길은 하나다 — Redis → DB (1.41.0).

왜 이 파일이 있는가
-------------------
같은 키를 계층마다 다른 저장소에서 읽고 있었다:

  · core 의 PersistentConfig : DB → Redis
  · 위성 파드의 ConfigClient : **Redis 만** (db_manager 없이 생성)
  · 일부 서비스            : persistent_configs 를 직접 SELECT

그래서 Redis 에서 키가 빠지는 순간 관리자 화면은 [설정됨] 인데 워크플로우 파드만
빈 값을 봤고, 그 빈 값이 그대로 provider SDK 로 들어가
"Could not resolve authentication method" 로 터졌다.

여기서 고정하는 것:
  1. Redis 에 없으면 DB 가 받아 주고, 받아 온 값은 Redis 로 되살아난다(version 은 안 올린다).
  2. 못 읽은 것은 "설정 안 함" 이 아니다 — status="error".
  3. 못 읽었을 때 기본값을 저장소에 **쓰지 않는다** (관리자 값 덮어쓰기 방지).
  4. Redis 가 죽어도 관리자의 저장은 DB 에 남는다.
  5. ConfigClient 는 DB 를 나중에 붙여도 같은 사다리를 탄다.
"""
from __future__ import annotations

import json

from xgen_sdk.config.base_config import PersistentConfig
from xgen_sdk.config.config_client import ConfigClient
from xgen_sdk.config.redis_config import RedisConfigManager


class _Redis:
    def __init__(self, alive=True, data=None):
        self.alive = alive
        self.data = dict(data or {})
        self.writes = []
        self.incrs = 0

    def _check(self):
        if not self.alive:
            raise ConnectionError("redis down")

    def ping(self):
        self._check()
        return True

    def get(self, key):
        self._check()
        return self.data.get(key)

    def set(self, key, value):
        self._check()
        self.data[key] = value
        self.writes.append(key)

    def sadd(self, key, member):
        self._check()
        self.data.setdefault(key, set()).add(member)

    def incr(self, key):
        self._check()
        self.incrs += 1
        return self.incrs


class _Db:
    """persistent_configs 한 장. execute_query_one 만 있으면 helper 가 읽는다."""

    def __init__(self, rows=None, alive=True):
        self.rows = list(rows or [])
        self.alive = alive
        self.db_type = "postgresql"
        self.saved = []

    def _is_pool_healthy(self):
        return self.alive

    def execute_query_one(self, query, params):
        if not self.alive:
            raise RuntimeError("db down")
        column = "config_path" if "config_path =" in query else "env_name"
        for row in self.rows:
            if row.get(column) == params[0]:
                return row
        return None

    def execute_query(self, query, params=None):
        if not self.alive:
            raise RuntimeError("db down")
        self.saved.append(params)
        return None

    def table_exists(self, name):
        return True


def _manager(redis_alive=True, redis_data=None, db=None):
    import time

    mgr = RedisConfigManager.__new__(RedisConfigManager)
    mgr.config_prefix = "config"
    mgr.version_key = "config:_meta:version"
    mgr.redis_client = _Redis(alive=redis_alive, data=redis_data)
    mgr._connection_available = redis_alive
    # 방금 시도한 것으로 둬서 쿨다운이 재접속을 막는다 — 죽은 상태를 고정한다.
    mgr._last_connect_attempt = time.monotonic()
    mgr._reconnect_cooldown = 999.0
    mgr.recovery_epoch = 0
    mgr._last_write_version = 0
    mgr._name_index_cache = None
    mgr._name_index_tail_cache = None
    mgr._name_index_version = -1
    mgr.db_manager = db
    return mgr


def _row(env_name, path, value, data_type="string"):
    return {"env_name": env_name, "config_path": path,
            "config_value": value, "data_type": data_type}


def test_redis_miss_falls_back_to_db_and_restores_the_key():
    db = _Db([_row("ANTHROPIC_API_KEY", "anthropic.api_key", "sk-real")])
    mgr = _manager(db=db)

    status, value, source = mgr.probe_value("ANTHROPIC_API_KEY", "anthropic.api_key")

    assert (status, value, source) == ("ok", "sk-real", "db")
    # 되살아났다 — 다음 읽기는 Redis 에서 뜬다.
    restored = json.loads(mgr.redis_client.data["config:ANTHROPIC_API_KEY"])
    assert restored["value"] == "sk-real"
    # 복구는 값의 변경이 아니다 — 다른 파드를 깨우지 않는다.
    assert mgr.redis_client.incrs == 0
    assert mgr.probe_value("ANTHROPIC_API_KEY")[2] == "redis"


def test_unreadable_stores_are_not_reported_as_unset():
    mgr = _manager(redis_alive=False, db=_Db(alive=False))
    assert mgr.probe_value("ANTHROPIC_API_KEY")[0] == "error"
    # 두 곳 다 정상인데 없으면 그건 진짜 "없음".
    assert _manager(db=_Db([])).probe_value("NOPE")[0] == "missing"


def test_default_is_not_written_when_the_store_could_not_answer():
    """저장소 장애 때 기본값을 쓰면 관리자가 넣어 둔 값이 그 순간 덮인다."""
    mgr = _manager(redis_alive=False, db=_Db(alive=False))
    cfg = PersistentConfig(
        env_name="ANTHROPIC_API_KEY", config_path="anthropic.api_key",
        env_value="", redis_manager=mgr, db_manager=mgr.db_manager,
    )
    assert cfg.value == ""            # 메모리에는 기본값
    assert mgr.redis_client.writes == []   # 저장소에는 아무것도 안 썼다
    assert mgr.db_manager.saved == []


def test_admin_save_survives_a_dead_redis():
    """Redis 쓰기 실패가 DB 쓰기를 막으면 관리자의 저장이 통째로 사라진다."""
    db = _Db([])
    mgr = _manager(redis_alive=False, db=db)

    assert mgr.set_config("anthropic.api_key", "sk-new",
                          env_name="ANTHROPIC_API_KEY") is True
    assert any("sk-new" in str(p) for p in db.saved), db.saved


def test_config_client_uses_the_same_ladder_once_db_is_attached():
    db = _Db([_row("OPENAI_API_KEY", "openai.api_key", "sk-openai")])
    client = ConfigClient(manager=_manager())

    assert client.get_config_by_name("OPENAI_API_KEY").value is None
    assert client.has_db_fallback is False

    assert client.attach_db_manager(db) is True
    found = client.get_config_by_name("OPENAI_API_KEY")
    assert (found.value, found.status, found.source) == ("sk-openai", "ok", "db")


def test_a_name_absent_from_both_stores_does_not_hammer_the_db():
    """폴백은 조용히 증폭된다 — 없는 이름을 반복 조회해도 DB 왕복은 한 번뿐이다."""
    db = _Db([])
    mgr = _manager(db=db)
    calls = []
    original = db.execute_query_one
    db.execute_query_one = lambda q, p: (calls.append(p[0]) or original(q, p))

    for _ in range(5):
        assert mgr.probe_value("NOPE", "no.pe")[0] == "missing"

    assert len(calls) == 2, calls   # config_path 1회 + env_name 1회, 그 뒤로는 기억
