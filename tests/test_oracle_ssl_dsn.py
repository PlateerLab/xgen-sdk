"""
Oracle TCPS/SSL dsn 빌더 단위 테스트 (db_connections.dsn_builder).

실행: python tests/test_oracle_ssl_dsn.py (또는 pytest)
"""
from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from xgen_sdk.db_connections.dsn_builder import (  # noqa: E402
    build_oracle_dsn,
    build_oracle_description_lb,
    build_oracle_easy_connect,
    build_oracle_sqlalchemy,
    build_sqlalchemy,
)

H1 = [("db1", 1521)]
H2 = [("db1", 1521), ("db2", 1522)]


def test_easy_connect_ssl_prefix():
    """use_ssl=True → tcps:// 접두, 기본은 무접두 (회귀 보존)."""
    assert build_oracle_easy_connect(H2, "svc") == "db1:1521,db2:1522/svc"
    assert build_oracle_easy_connect(H2, "svc", use_ssl=True) == "tcps://db1:1521,db2:1522/svc"


def test_description_lb_tcps_protocol():
    plain = build_oracle_description_lb(H2, "svc", load_balance=True)
    tcps = build_oracle_description_lb(H2, "svc", load_balance=True, use_ssl=True)
    assert "(PROTOCOL=TCP)" in plain and "TCPS" not in plain
    assert "(PROTOCOL=TCPS)" in tcps and "(PROTOCOL=TCP)" not in tcps.replace("TCPS", "")


def test_build_oracle_dsn_ssl():
    assert build_oracle_dsn(H1, "svc") == "db1:1521/svc"
    assert build_oracle_dsn(H1, "svc", use_ssl=True) == "tcps://db1:1521/svc"
    assert build_oracle_dsn(H2, "svc", use_ssl=True) == "tcps://db1:1521,db2:1522/svc"
    lb = build_oracle_dsn(H2, "svc", options={"multi_host_mode": "load_balance"}, use_ssl=True)
    assert "(PROTOCOL=TCPS)" in lb


def test_sqlalchemy_non_ssl_unchanged():
    """비 SSL 단일 host — 기존 URL 형태 그대로 (회귀 보존)."""
    url, ca = build_oracle_sqlalchemy(H1, "svc", "u", "p")
    assert url.host == "db1" and url.port == 1521
    assert dict(url.query) == {"service_name": "svc"}
    assert ca == {}


def test_sqlalchemy_ssl_uses_dsn():
    """SSL 이면 단일 host 도 dsn 경유 (URL 문법으로 TCPS 표현 불가)."""
    url, ca = build_oracle_sqlalchemy(H1, "svc", "u", "p", use_ssl=True)
    assert url.host is None and url.port is None
    assert ca["dsn"] == "tcps://db1:1521/svc"
    assert "ssl_server_dn_match" not in ca  # verify 모드 아님


def test_sqlalchemy_ssl_verify_dn_match():
    for mode in ("verify-ca", "verify-full"):
        _, ca = build_oracle_sqlalchemy(H1, "svc", "u", "p", use_ssl=True, ssl_mode=mode)
        assert ca.get("ssl_server_dn_match") is True
    # require 등은 dn match 없이 TCPS 만
    _, ca = build_oracle_sqlalchemy(H1, "svc", "u", "p", use_ssl=True, ssl_mode="require")
    assert "ssl_server_dn_match" not in ca


def test_sqlalchemy_multi_host_ssl():
    url, ca = build_oracle_sqlalchemy(H2, "svc", "u", "p", use_ssl=True)
    assert url.host is None
    assert ca["dsn"].startswith("tcps://")


def test_build_sqlalchemy_dispatch_passes_ssl():
    """통합 빌더 — oracle 분기가 ssl_mode/use_ssl 을 소비한다."""
    url, ca = build_sqlalchemy(
        "oracle", "db1", 1521, "svc", "u", "p", ssl_mode="require",
    )
    assert ca.get("dsn") == "tcps://db1:1521/svc"
    # ssl 미지정 시 기존 동작
    url2, ca2 = build_sqlalchemy("oracle", "db1", 1521, "svc", "u", "p")
    assert url2.host == "db1" and ca2 == {}


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print()
    print(f"{len(fns) - len(failed)}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
