"""DB 폴백의 **실제 SQL** 왕복 — 가짜 객체로는 오타가 잡히지 않는다.

앞의 계약 테스트들은 execute_query 를 흉내 낸 객체로 사다리의 **판단**을 본다.
그것만으로는 컬럼 이름이 틀렸거나 placeholder 가 어긋나도 통과한다. 여기서는
진짜 sqlite 에 persistent_configs 를 만들고 조회·쓰기·삭제·카테고리를 돌린다.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from xgen_sdk.config.redis_config import RedisConfigManager
from xgen_sdk.db.db_config_helper import (
    delete_db_config,
    ensure_config_table_exists,
    probe_db_category_rows,
    probe_db_config_row,
    set_db_config,
)


class _SqliteManager:
    """DatabaseManagerPsycopg3 의 sqlite 형태 — helper 가 쓰는 표면만."""

    db_type = "sqlite"

    def __init__(self, con):
        self._sqlite_connection = con

    def execute_query(self, query, params=None):
        cur = self._sqlite_connection.execute(query, params or ())
        self._sqlite_connection.commit()
        if query.strip().upper().startswith("SELECT"):
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        return None

    def execute_query_one(self, query, params=None):
        rows = self.execute_query(query, params)
        return rows[0] if rows else None

    def table_exists(self, name):
        row = self._sqlite_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None


class _DeadRedis:
    def ping(self):
        raise ConnectionError("down")

    def get(self, key):
        raise ConnectionError("down")


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    mgr = _SqliteManager(con)
    assert ensure_config_table_exists(mgr) is True
    yield mgr
    con.close()


@pytest.fixture
def manager(db):
    """Redis 는 죽어 있고 DB 만 있는 매니저 — 폴백 경로를 그대로 태운다."""
    mgr = RedisConfigManager.__new__(RedisConfigManager)
    mgr.config_prefix = "config"
    mgr.version_key = "config:_meta:version"
    mgr.redis_client = _DeadRedis()
    mgr._connection_available = False
    mgr._last_connect_attempt = time.monotonic()
    mgr._reconnect_cooldown = 999.0
    mgr.recovery_epoch = 0
    mgr._last_write_version = 0
    mgr._name_index_cache = None
    mgr._name_index_tail_cache = None
    mgr._name_index_version = -1
    mgr.db_manager = db
    return mgr


def test_write_then_read_goes_through_real_sql(db, manager):
    assert manager.set_config(
        "anthropic.api_key", "sk-real", data_type="string",
        env_name="ANTHROPIC_API_KEY",
    ) is True                                   # Redis 는 죽었지만 DB 에 남는다

    status, row = probe_db_config_row(db, config_path="anthropic.api_key")
    assert (status, row["value"]) == ("ok", "sk-real")

    # env_name 으로도 찾는다 — 호출부가 가진 이름이 무엇인지에 따라 갈린다.
    assert probe_db_config_row(db, env_name="ANTHROPIC_API_KEY")[1]["value"] == "sk-real"

    # 사다리 전체
    assert manager.probe_value("ANTHROPIC_API_KEY", "anthropic.api_key") == (
        "ok", "sk-real", "db",
    )
    assert manager.get_config_value("ANTHROPIC_API_KEY") == "sk-real"
    assert manager.get_config_by_name("ANTHROPIC_API_KEY") == "sk-real"


def test_types_survive_the_round_trip(db, manager):
    set_db_config(db, "llm.models", ["a", "b"], "list", "LLM_MODELS")
    set_db_config(db, "llm.enabled", True, "bool", "LLM_ENABLED")
    set_db_config(db, "llm.limit", 42, "int", "LLM_LIMIT")

    assert probe_db_config_row(db, config_path="llm.models")[1]["value"] == ["a", "b"]
    assert probe_db_config_row(db, config_path="llm.enabled")[1]["value"] is True
    assert probe_db_config_row(db, config_path="llm.limit")[1]["value"] == 42


def test_category_rows_come_back_from_real_sql(db):
    set_db_config(db, "vectordb.host", "10.0.0.9", "string", "VECTORDB_HOST")
    set_db_config(db, "vectordb.port", 6333, "int", "VECTORDB_PORT")
    set_db_config(db, "openai.api_key", "sk-x", "string", "OPENAI_API_KEY")

    status, rows = probe_db_category_rows(db, "vectordb")
    assert status == "ok"
    assert {r["path"] for r in rows} == {"vectordb.host", "vectordb.port"}
    assert {r["env_name"] for r in rows} == {"VECTORDB_HOST", "VECTORDB_PORT"}


def test_delete_really_removes_the_row(db, manager):
    set_db_config(db, "legacy.key", "v", "string", "LEGACY_KEY")
    assert delete_db_config(db, config_path="legacy.key", env_name="LEGACY_KEY") is True
    assert probe_db_config_row(db, config_path="legacy.key")[0] == "missing"
    # 사다리의 DB 단도 "없음" — 되살릴 값이 남아 있지 않다.
    assert manager._probe_db_row("LEGACY_KEY", "legacy.key")[0] == "missing"
    # 참고: 이 픽스처는 Redis 가 죽어 있으므로 probe_value 전체는 "error" 다.
    # 한 쪽을 못 읽는 동안 "설정 없음" 이라고 단정하지 않는 것이 이 계층의 계약이다.
    assert manager.probe_value("LEGACY_KEY", "legacy.key")[0] == "error"


def test_a_dead_db_is_an_error_not_an_absence(db, manager):
    set_db_config(db, "app.switch", "true", "string", "APP_SWITCH")
    db._sqlite_connection.close()               # DB 가 죽는다
    status, _value, _src = manager.probe_value("APP_SWITCH", "app.switch")
    assert status == "error", "저장소 장애가 '설정 안 함' 으로 둔갑하면 보호가 풀린다"
