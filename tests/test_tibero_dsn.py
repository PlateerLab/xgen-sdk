"""Tibero ODBC 연결 문자열 — 값이 문자열을 갈라 놓지 못하게 한다.

Tibero 는 SQL·데이터 딕셔너리가 Oracle 호환이지만 **와이어 프로토콜이 다르다**.
oracledb 로는 못 붙고 공식 Python 드라이버도 없어서, Informix 와 같은 길을 간다:
표준 래퍼(pyodbc) + 벤더 클라이언트(libtbodbc.so).

ODBC 연결 문자열은 ``키=값;`` 을 이어 붙인 평문이다. 값에 ``;`` 가 들어가면 그
지점에서 새 키가 시작된 것으로 읽혀 **뒤가 통째로 잘리거나 엉뚱한 서버로 붙는다** —
비밀번호에 세미콜론을 쓰는 계정은 실제로 있다. 그래서 조립 단계에서 막는다.
"""

from __future__ import annotations

import pytest

from xgen_sdk.db_connections.dsn_builder import (
    DEFAULT_PORTS,
    SUPPORTED_DB_TYPES,
    InvalidHostsError,
    MultiHostNotSupportedError,
    build_tibero_odbc_conn_str,
)


def test_tibero_is_a_supported_db_type() -> None:
    assert "tibero" in SUPPORTED_DB_TYPES


def test_default_port_is_tibero_s_own_not_oracle_s() -> None:
    """8629 다. Oracle 호환이라는 이유로 1521 을 쓰면 아무 데도 안 붙는다."""
    assert DEFAULT_PORTS["tibero"] == 8629


def test_builds_a_driver_connection_string() -> None:
    out = build_tibero_odbc_conn_str([("10.0.0.9", 8629)], "tibero", "app", "pw")
    assert out == "DRIVER={Tibero};SERVER=10.0.0.9;PORT=8629;DB=tibero;UID=app;PWD=pw;"


def test_driver_name_is_overridable() -> None:
    """사이트마다 odbcinst.ini 등록 이름이 다르다 — 'Tibero 7' 처럼 공백도 흔하다."""
    out = build_tibero_odbc_conn_str(
        [("h", 8629)], "db", "u", "p", {"tibero_driver": "Tibero 7"},
    )
    assert "DRIVER={Tibero 7};" in out


def test_a_preregistered_dsn_wins_over_host_and_port() -> None:
    """사내 표준 DSN 을 이미 배포한 사이트를 위한 길."""
    out = build_tibero_odbc_conn_str(
        [("ignored", 9999)], "db", "u", "p", {"tibero_dsn": "TB_PROD"},
    )
    assert out.startswith("DSN=TB_PROD;")
    assert "SERVER=" not in out and "PORT=" not in out


@pytest.mark.parametrize("bad", ["p;DROP", "p{x}", "a}b"])
def test_separator_characters_are_refused_not_silently_kept(bad: str) -> None:
    """조용히 잘린 문자열로 엉뚱한 서버에 붙는 것보다 못 붙는 편이 낫다."""
    with pytest.raises(InvalidHostsError):
        build_tibero_odbc_conn_str([("h", 8629)], "db", "u", bad)


def test_injection_is_blocked_on_every_value_not_just_password() -> None:
    for kwargs in (
        {"db_name": "d;X"},
        {"username": "u;X"},
    ):
        with pytest.raises(InvalidHostsError):
            build_tibero_odbc_conn_str(
                [("h", 8629)],
                kwargs.get("db_name", "db"),
                kwargs.get("username", "u"),
                "p",
            )
    with pytest.raises(InvalidHostsError):
        build_tibero_odbc_conn_str([("h;X", 8629)], "db", "u", "p")


def test_multi_host_is_refused() -> None:
    """ODBC 연결 문자열에 여러 SERVER 를 적을 자리가 없다 — 침묵하면 첫 host 만 쓴다."""
    with pytest.raises(MultiHostNotSupportedError):
        build_tibero_odbc_conn_str([("h1", 8629), ("h2", 8629)], "db", "u", "p")


def test_empty_credentials_are_omitted_not_sent_blank() -> None:
    """빈 UID/PWD 를 보내면 드라이버가 '빈 계정' 으로 시도해 오류 메시지가 엉뚱해진다."""
    out = build_tibero_odbc_conn_str([("h", 8629)], "db", "", "")
    assert "UID=" not in out and "PWD=" not in out
