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
        # 진짜 Redis 처럼 값을 남긴다 — 남기지 않으면 다른 파드의 GET 이 못 본다.
        self.data[key] = str(self.incrs)
        return self.incrs

    def delete(self, key):
        self._check()
        self.data.pop(key, None)

    def srem(self, key, member):
        self._check()
        bucket = self.data.get(key)
        if isinstance(bucket, set):
            bucket.discard(member)


class _Db:
    """persistent_configs 한 장. execute_query_one 만 있으면 helper 가 읽는다."""

    def __init__(self, rows=None, alive=True):
        self.rows = list(rows or [])
        self.alive = alive
        self.db_type = "postgresql"
        self.saved = []
        self.deleted = []

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
        if query.strip().upper().startswith("SELECT"):
            prefix = params[0].rstrip("%")
            return [r for r in self.rows if (r.get("config_path") or "").startswith(prefix)]
        if query.strip().upper().startswith("DELETE"):
            self.deleted.append(params[0])
            column = "config_path" if "config_path =" in query else "env_name"
            self.rows = [r for r in self.rows if r.get(column) != params[0]]
            return None
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


def test_a_transient_outage_does_not_flip_settings_back_to_defaults():
    """갱신 때 못 읽었다고 기본값을 박으면, 파드의 모든 설정이 조용히 뒤집힌다.

    시나리오: Redis 재연결 직후 refresh_all 이 도는데 저장소가 아직 흔들린다.
    """
    db = _Db([_row("SESSION_MIN", "app.session_min", "60", "int")])
    mgr = _manager(db=db)
    cfg = PersistentConfig(
        env_name="SESSION_MIN", config_path="app.session_min",
        env_value=480, redis_manager=mgr, db_manager=db,
    )
    assert cfg.value == 60           # 관리자가 넣어 둔 값

    mgr.redis_client.alive = False   # 저장소가 통째로 흔들린다
    db.alive = False
    cfg.refresh()

    assert cfg.value == 60, "갱신 실패가 값을 등록 기본값(480)으로 뒤집었다"

    # 저장소가 돌아오면 다음 접근에서 스스로 다시 읽는다.
    mgr.redis_client.alive = True
    db.alive = True
    db.rows[0]["config_value"] = "90"
    mgr.redis_client.data.clear()
    assert cfg.value == 90


def test_a_write_survives_a_key_missing_from_redis():
    """Redis 에서 키가 빠졌다고 쓰기가 실패하면 안 된다 — 값은 DB 에 있다.

    위성 파드의 쓰기(예: CLI 로그인 토큰 저장)가 이 경로를 탄다. 예전에는
    Redis 메타가 없으면 전체 스윕 → KeyError 로 끝났다.
    """
    db = _Db([_row("CLAUDE_CODE_OAUTH_TOKEN", "claude_code.oauth_token", "old")])
    mgr = _manager(db=db)          # Redis 는 살아 있지만 이 키가 없다

    mgr.update_config_by_name("CLAUDE_CODE_OAUTH_TOKEN", "new-token")

    written = json.loads(mgr.redis_client.data["config:CLAUDE_CODE_OAUTH_TOKEN"])
    assert written["value"] == "new-token"
    assert written["path"] == "claude_code.oauth_token"   # DB 메타를 그대로 이어받는다
    assert any("new-token" in str(p) for p in db.saved), db.saved


def test_delete_removes_the_row_from_both_stores():
    """Redis 에서만 지우면 다음 조회가 DB 에서 값을 찾아 되살린다."""
    db = _Db([_row("LEGACY_KEY", "legacy.key", "v")])
    mgr = _manager(redis_data={"config:LEGACY_KEY": json.dumps(
        {"value": "v", "type": "string", "category": "legacy",
         "path": "legacy.key", "env_name": "LEGACY_KEY"})}, db=db)

    assert mgr.delete_config("LEGACY_KEY") is True
    assert db.deleted, "DB 행이 남아 있으면 삭제한 설정이 되살아난다"
    assert mgr.probe_value("LEGACY_KEY", "legacy.key")[0] == "missing"


def test_category_reads_take_the_same_ladder():
    """카테고리만 Redis 전용이면, 그 셋이 빠지는 순간 '설정이 하나도 없다' 가 된다."""
    db = _Db([
        _row("VECTORDB_HOST", "vectordb.host", "10.0.0.9"),
        _row("VECTORDB_PORT", "vectordb.port", "6333", "int"),
        _row("OPENAI_API_KEY", "openai.api_key", "sk-x"),
    ])
    mgr = _manager(db=db)          # Redis 에 카테고리 셋이 없다

    rows = mgr.get_category_configs("vectordb")

    assert {r["path"] for r in rows} == {"vectordb.host", "vectordb.port"}
    # 되살아났으니 다음 조회는 Redis 에서 뜬다.
    assert mgr.probe_value("VECTORDB_HOST")[2] == "redis"


def test_an_admin_update_reaches_another_pod_immediately():
    """전파 계약 — 관리자가 바꾸면 다른 파드가 **다음 조회에서** 새 값을 본다.

    · 위성 파드(ConfigClient)는 캐시가 없다 → 즉시.
    · 등록 파드(PersistentConfig)는 version sentinel 로 drift 를 보고 스스로 다시 읽는다.
    """
    shared_redis = _Redis()
    db = _Db([_row("MODEL_DEFAULT", "llm.model_default", "gpt-4o")])

    def pod():
        mgr = _manager(db=db)
        mgr.redis_client = shared_redis      # 두 파드가 같은 Redis 를 본다
        return mgr

    admin, satellite = pod(), pod()
    holder = PersistentConfig(
        env_name="MODEL_DEFAULT", config_path="llm.model_default",
        env_value="", redis_manager=pod(), db_manager=db,
    )
    holder.redis_manager.redis_client = shared_redis
    assert holder.value == "gpt-4o"
    assert satellite.get_config_value("MODEL_DEFAULT") == "gpt-4o"

    admin.set_config("llm.model_default", "claude-sonnet-4-6",
                     env_name="MODEL_DEFAULT")

    # 위성: 캐시가 없으니 곧바로 새 값
    assert satellite.get_config_value("MODEL_DEFAULT") == "claude-sonnet-4-6"
    # 등록 파드: version 이 올랐으니 다음 .value 접근에서 스스로 다시 읽는다
    from xgen_sdk.config.base_config import _invalidate_version_cache
    _invalidate_version_cache()
    assert holder.value == "claude-sonnet-4-6"
    # 영속 저장소에도 남았다
    assert any("claude-sonnet-4-6" in str(x) for x in db.saved), db.saved
