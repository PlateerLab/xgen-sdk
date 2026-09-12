"""제약 동기화가 **테이블 레벨 UNIQUE 선언**을 지키는가.

`UNIQUE_x: 'UNIQUE(col)'` 로 선언한 유일 제약은 create_tables 가 만들지만, 컬럼
정의만 보는 동기화가 "모델에 없는 제약" 으로 판단해 **떨어뜨렸다.** 만든 직후
지우는 셈이라 `ON CONFLICT (col)` 이 죽는다 — 결재 정책 표가 그렇게 죽었다.
"""
from __future__ import annotations

from xgen_sdk.db.pool_manager import DatabaseManagerPsycopg3 as M


class Recorder(M):
    """DB 없이 동기화가 **무슨 DDL 을 내는지**만 받아 적는 대역."""

    def __init__(self, columns, unique_now):
        # 부모 __init__ 는 접속을 만드므로 부르지 않는다
        self.db_type = "postgresql"
        import logging
        self.logger = logging.getLogger("t")
        self._cols = columns
        self._unique_now = set(unique_now)
        self.ddl = []

    def _get_table_columns_full(self, table_name):
        return {c: {"is_nullable": "NO", "column_default": None} for c in self._cols}

    def _get_unique_columns(self, table_name):
        return set(self._unique_now)

    def _execute_ddl_strict(self, sql):
        self.ddl.append(" ".join(sql.split()))
        return True

    def execute_query(self, sql, params=None):
        if "pg_constraint" in sql:
            return [{"conname": "unique_action"}]
        return []


SCHEMA = {
    "action_type": "VARCHAR(64) NOT NULL",
    "required": "BOOLEAN NOT NULL DEFAULT FALSE",
    "UNIQUE_action": "UNIQUE(action_type)",
}
COLS = {"action_type", "required"}


def test_it_reads_single_column_table_level_unique_declarations():
    assert M._declared_table_unique_columns(SCHEMA) == {"action_type"}
    # 다중 컬럼은 단일 컬럼 동기화의 대상이 아니다
    assert M._declared_table_unique_columns({"UNIQUE_pair": "UNIQUE(a, b)"}) == set()


def test_a_declared_table_level_unique_is_not_dropped():
    """이게 결재 정책 표를 죽인 그 경로다."""
    db = Recorder(COLS, unique_now={"action_type"})
    db._run_constraint_migrations("approval_action_policies", SCHEMA, COLS)
    assert not any("DROP CONSTRAINT" in d or "DROP INDEX" in d for d in db.ddl), db.ddl


def test_a_missing_declared_unique_is_restored():
    """이미 떨어진 표(운영 dev)도 다음 기동에서 되살아나야 한다."""
    db = Recorder(COLS, unique_now=set())
    db._run_constraint_migrations("approval_action_policies", SCHEMA, COLS)
    assert any("ADD CONSTRAINT" in d and "UNIQUE (action_type)" in d for d in db.ddl), db.ddl


def test_a_unique_index_declared_via_get_indexes_is_kept():
    """get_indexes() 의 3-튜플 유일 인덱스(예: deploy_meta.workflow_id)도 같다."""
    schema = {"workflow_id": "VARCHAR(128) NOT NULL"}
    db = Recorder({"workflow_id"}, unique_now={"workflow_id"})
    db._run_constraint_migrations("deploy_meta", schema, {"workflow_id"},
                                  declared_unique_columns={"workflow_id"})
    assert not any("DROP" in d for d in db.ddl), db.ddl


def test_an_undeclared_unique_is_still_dropped():
    """동기화의 원래 일은 그대로다 — 모델이 정말로 유일을 선언하지 않은 컬럼은 푼다."""
    db = Recorder({"name"}, unique_now={"name"})
    db._run_constraint_migrations("t", {"name": "VARCHAR(10)"}, {"name"})
    assert any("DROP CONSTRAINT" in d for d in db.ddl), db.ddl
