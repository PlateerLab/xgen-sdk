"""SEC-12 — XgenDB 조건절 조립 시 식별자(테이블·컬럼·정렬 키) 화이트리스트 검증.

값(value)은 드라이버 파라미터로 바인딩되므로 안전했지만, 조건 키·orderby·컬럼 목록은
쿼리 문자열에 직접 삽입되어 상위 서비스가 요청 파라미터를 그대로 넘기면 SQL 주입
지점이 됐다. sql_identifiers 모듈이 fail-closed 로 거부하는지, XgenDB 가 실제 조립
전에 그 검증을 거치는지 확인한다.
"""

import unittest
from unittest.mock import MagicMock

import pytest

from xgen_sdk.db.sql_identifiers import (
    UnsafeSqlIdentifierError,
    split_condition_key,
    validate_column_list,
    validate_orderby,
    validate_table_name,
)


class SqlIdentifierTests(unittest.TestCase):
    def test_table_name_accepts_plain_and_schema_qualified(self):
        self.assertEqual(validate_table_name("users"), "users")
        self.assertEqual(validate_table_name("public.users"), "public.users")

    def test_table_name_rejects_injection(self):
        for bad in ("users; DROP TABLE users", "users--", "users WHERE 1=1", "", None, "a.b.c", "users)"):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafeSqlIdentifierError):
                    validate_table_name(bad)

    def test_condition_key_splits_operator_and_validates_column(self):
        self.assertEqual(split_condition_key("user_id"), ("user_id", ""))
        self.assertEqual(split_condition_key("user_id__in__"), ("user_id", "__in__"))
        self.assertEqual(split_condition_key("name__notlike__"), ("name", "__notlike__"))
        self.assertEqual(split_condition_key("t.status__not__"), ("t.status", "__not__"))

    def test_condition_key_rejects_injection(self):
        for bad in (
            "1=1 OR user_id",
            "user_id = 1; --",
            "user_id) OR (1=1",
            "user_id__in__ OR 1=1",
            "",
            "__in__",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafeSqlIdentifierError):
                    split_condition_key(bad)

    def test_orderby_accepts_simple_and_multi_column(self):
        self.assertEqual(validate_orderby("id"), "id")
        self.assertEqual(validate_orderby("created_at desc"), "created_at DESC")
        self.assertEqual(validate_orderby("t.priority ASC, t.id DESC"), "t.priority ASC, t.id DESC")
        self.assertEqual(
            validate_orderby("updated_at DESC NULLS LAST, id DESC"), "updated_at DESC NULLS LAST, id DESC"
        )
        self.assertEqual(validate_orderby("updated_at nulls first"), "updated_at NULLS FIRST")
        self.assertEqual(validate_orderby(""), "id")
        self.assertEqual(validate_orderby(None), "id")

    def test_orderby_rejects_injection(self):
        for bad in (
            "id; DROP TABLE users",
            "(SELECT 1)",
            "id DESC, (CASE WHEN 1=1 THEN id END)",
            "id NULLS",
            "id NULLS MIDDLE",
            "id DESC NULLS LAST extra",
            "id DESC --",
            "pg_sleep(5)",
            "id ASC DESC",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafeSqlIdentifierError):
                    validate_orderby(bad)

    def test_column_list(self):
        self.assertIsNone(validate_column_list(None))
        self.assertEqual(validate_column_list(["id", "t.name"]), ["id", "t.name"])
        with self.assertRaises(UnsafeSqlIdentifierError):
            validate_column_list(["id", "password FROM users --"])


psycopg = pytest.importorskip("psycopg")  # XgenDB 는 psycopg3 를 import 한다

from xgen_sdk.db.app_db import XgenDB  # noqa: E402


def _make_db():
    """실제 커넥션 없이 쿼리 문자열만 캡처하는 XgenDB."""
    db = XgenDB.__new__(XgenDB)
    db.logger = MagicMock()
    db._models_registry = []
    db._max_retries = 0
    db._retry_delay = 0
    db._retry_backoff = 1
    db._last_health_check = 0
    db._health_check_interval = 30
    db._auto_recover = False
    mgr = MagicMock()
    mgr.db_type = "postgresql"
    mgr.execute_query.return_value = []
    mgr.execute_update_delete.return_value = 1
    mgr.execute_insert.return_value = 1
    db.config_db_manager = mgr
    return db, mgr


class XgenDBConditionAssemblyTests(unittest.TestCase):
    def test_find_records_by_condition_binds_values_and_validates_keys(self):
        db, mgr = _make_db()
        db.find_records_by_condition(
            "chat_sessions",
            {"user_id": 7, "status__in__": ["a", "b"], "title__like__": "x' OR 1=1 --"},
            orderby="updated_at desc, id",
            limit=10,
            offset=5,
        )
        query, params = mgr.execute_query.call_args[0]
        self.assertEqual(
            query,
            "SELECT * FROM chat_sessions WHERE user_id = %s AND status IN (%s, %s) AND title LIKE %s "
            "ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s",
        )
        # 값은 쿼리 문자열에 절대 들어가지 않고 파라미터로만 전달된다
        self.assertEqual(params, (7, "a", "b", "%x' OR 1=1 --%", 10, 5))

    def test_find_records_by_condition_rejects_unsafe_condition_key(self):
        db, mgr = _make_db()
        with self.assertRaises(UnsafeSqlIdentifierError):
            db.find_records_by_condition("chat_sessions", {"user_id = 1 OR 1=1 --": 7})
        mgr.execute_query.assert_not_called()

    def test_find_records_by_condition_rejects_unsafe_orderby_and_table(self):
        db, mgr = _make_db()
        with self.assertRaises(UnsafeSqlIdentifierError):
            db.find_records_by_condition("chat_sessions", {"user_id": 1}, orderby="id; DROP TABLE users")
        with self.assertRaises(UnsafeSqlIdentifierError):
            db.find_records_by_condition("chat_sessions; --", {"user_id": 1})
        with self.assertRaises(UnsafeSqlIdentifierError):
            db.find_records_by_condition("chat_sessions", {"user_id": 1}, select_columns=["id", "1) FROM users --"])
        mgr.execute_query.assert_not_called()

    def test_join_user_prefixes_alias_for_conditions_and_orderby(self):
        db, mgr = _make_db()
        db.find_records_by_condition(
            "workflows", {"user_id": 1, "deleted_at__isnull__": True}, orderby="priority, id DESC", join_user=True
        )
        query, params = mgr.execute_query.call_args[0]
        self.assertEqual(
            query,
            "SELECT t.*, u.username, u.full_name FROM workflows t LEFT JOIN users u ON t.user_id = u.id "
            "WHERE t.user_id = %s AND t.deleted_at IS NULL ORDER BY t.priority DESC, t.id DESC LIMIT %s OFFSET %s",
        )
        self.assertEqual(params, (1, 500, 0))

    def test_orderby_nulls_last_keeps_default_direction_placement(self):
        db, mgr = _make_db()
        db.find_records_by_condition("users", {"id__gt__": 0}, orderby="updated_at DESC NULLS LAST, id")
        query, _ = mgr.execute_query.call_args[0]
        self.assertIn("ORDER BY updated_at DESC NULLS LAST, id DESC LIMIT", query)
        db.find_records_by_condition("users", {}, orderby="updated_at NULLS LAST", orderby_asc=True)
        query, _ = mgr.execute_query.call_args[0]
        self.assertIn("WHERE 1=1 ORDER BY updated_at ASC NULLS LAST LIMIT", query)

    def test_find_records_returns_error_dict_on_unsafe_input(self):
        db, mgr = _make_db()
        result = db.find_records("users", orderby="id) UNION SELECT password FROM users --")
        self.assertFalse(result["success"])
        self.assertIn("Unsafe SQL", result["error"])
        mgr.execute_query.assert_not_called()

    def test_update_and_delete_by_condition_refuse_empty_where(self):
        db, mgr = _make_db()
        result = db.update_records_by_condition("users", {"is_active": False}, {})
        self.assertFalse(result["success"])
        result = db.delete_records_by_condition("users", {"id__in__": []})
        self.assertFalse(result["success"])
        mgr.execute_update_delete.assert_not_called()

    def test_update_records_by_condition_validates_set_columns(self):
        db, mgr = _make_db()
        result = db.update_records_by_condition("users", {"is_admin = true, name": "x"}, {"id": 1})
        self.assertFalse(result["success"])
        mgr.execute_update_delete.assert_not_called()

        result = db.update_records_by_condition("users", {"full_name": "x"}, {"id": 1, "status__not__": "deleted"})
        self.assertTrue(result["success"])
        query, params = mgr.execute_update_delete.call_args[0]
        self.assertEqual(query, "UPDATE users SET full_name = %s WHERE id = %s AND status != %s")
        self.assertEqual(params, ("x", 1, "deleted"))

    def test_insert_record_validates_column_names(self):
        db, mgr = _make_db()
        result = db.insert_record("users", {"username": "a", "is_admin) VALUES (true) --": "b"})
        self.assertFalse(result["success"])
        mgr.execute_insert.assert_not_called()

    def test_unsafe_identifier_is_not_retried(self):
        db, mgr = _make_db()
        db._max_retries = 3
        # 'connection' 이라는 단어가 들어가도 재시도 로직(키워드 매칭)에 걸리지 않고 즉시 거부돼야 한다
        with self.assertRaises(UnsafeSqlIdentifierError):
            db.find_records_by_condition("users", {"connection; --": 1})
        mgr.execute_query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
