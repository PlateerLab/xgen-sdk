"""
외부 DB 연결의 URL/DSN 통합 빌더.

이전엔 URL/DSN 조립이 backend 9곳에 분산돼 있어 (engine_factory, database_loader,
database_query_reader, dbConnectionController, dbDocumentationController, db_sync_config 등)
multi-host 도입 시 한 곳만 빠뜨려도 회귀. 본 모듈로 단일 진실 통합.

지원 db_type:
- postgresql  (psycopg v3, libpq 14+ — target_session_attrs/load_balance_hosts 지원)
- oracle      (python-oracledb thin mode — Easy Connect Plus / DESCRIPTION_LIST)
- mysql       (PyMySQL — multi-host 미지원, 명시 reject)
- informix    (IfxPy — multi-host 미지원, 명시 reject)

Multi-host 표현:
- options JSONB 컬럼의 `additional_hosts: [{host, port}]` 에 보조 host 저장
- options.multi_host_mode: "failover" (기본) | "load_balance"
- primary = db_host/db_port 컬럼 그대로 (단일 host 회귀 0)

PostgreSQL multi-host 안전 패턴 (Risk #1b 검증):
- 공통 port: URL.create(host="h1,h2,h3", port=공통)
- 다른 port: URL.create(no host/port) + connect_args={"host": csv, "port": csv}
  → SQLAlchemy URL parser 의 port 자리 콤마 거부 우회

Oracle multi-host 안전 패턴 (Risk #3 검증):
- Easy Connect Plus dsn (`h1:p1,h2:p2/svc`) 를 connect_args 로 전달
- LOAD_BALANCE 모드 시 DESCRIPTION_LIST 풀 형식 자동 조립
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from sqlalchemy.engine import URL

logger = logging.getLogger("db-connection-dsn-builder")


# ─────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────

DEFAULT_PORTS: Dict[str, int] = {
    "postgresql": 5432,
    "oracle": 1521,
    "mysql": 3306,
    "informix": 9089,
}

MULTI_HOST_SUPPORTED_TYPES = frozenset({"postgresql", "oracle"})
SUPPORTED_DB_TYPES = frozenset({"postgresql", "oracle", "mysql", "informix"})

# multi_host_mode 허용 값
VALID_MULTI_HOST_MODES = frozenset({"failover", "load_balance"})


# ─────────────────────────────────────────────────────────────
# 예외
# ─────────────────────────────────────────────────────────────

class UnsupportedDBTypeError(ValueError):
    """지원하지 않는 db_type."""


class MultiHostNotSupportedError(ValueError):
    """multi-host 미지원 db_type 에 multi-host 가 지정됨."""


class InvalidHostsError(ValueError):
    """hosts 배열의 형식/값 오류."""


# ─────────────────────────────────────────────────────────────
# Hosts 파싱/검증
# ─────────────────────────────────────────────────────────────

def parse_options_dict(options: Any) -> Dict[str, Any]:
    """options 컬럼 (TEXT or dict) 을 dict 로 정규화."""
    if not options:
        return {}
    if isinstance(options, dict):
        return options
    if isinstance(options, str):
        try:
            parsed = json.loads(options)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            logger.warning("options JSON parse 실패 — 빈 dict 로 fallback")
    return {}


def extract_additional_hosts(options: Any) -> List[Dict[str, Any]]:
    """options dict 에서 additional_hosts 추출 + 정규화."""
    opts = parse_options_dict(options)
    raw = opts.get("additional_hosts") or []
    if not isinstance(raw, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        h = (item.get("host") or "").strip()
        p = item.get("port")
        if not h or p is None:
            continue
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        if not (1 <= p <= 65535):
            continue
        cleaned.append({"host": h, "port": p})
    return cleaned


def extract_multi_host_mode(options: Any) -> str:
    """options.multi_host_mode 추출. 기본값 'failover'."""
    opts = parse_options_dict(options)
    mode = (opts.get("multi_host_mode") or "failover").lower()
    if mode not in VALID_MULTI_HOST_MODES:
        return "failover"
    return mode


def parse_hosts(
    db_host: Optional[str],
    db_port: Optional[int],
    additional_hosts: Optional[List[Dict[str, Any]]],
    db_type: str,
) -> List[Tuple[str, int]]:
    """
    primary (db_host/db_port) + additional_hosts → [(host, port), ...] 정규화.

    Args:
        db_host: primary host (DBConnection.db_host 컬럼)
        db_port: primary port (DBConnection.db_port 컬럼)
        additional_hosts: options.additional_hosts 에서 추출한 dict list (또는 None)
        db_type: default port 결정용

    Returns:
        [(host, port), ...] — 최소 1개, primary 가 항상 첫 element

    Raises:
        InvalidHostsError: host string 에 콤마/공백 등 위험 문자 포함, 빈 host, port 범위 오류
    """
    primary_host = (db_host or "").strip()
    if not primary_host:
        primary_host = "localhost"

    # host string 자체에 콤마 들어가면 UI 우회 시도 — reject
    if "," in primary_host:
        raise InvalidHostsError(
            f"db_host 에 콤마가 포함됨 ({primary_host!r}) — additional_hosts 컬럼을 사용하세요"
        )

    default_port = DEFAULT_PORTS.get((db_type or "").lower(), 5432)
    primary_port = int(db_port) if db_port else default_port

    if not (1 <= primary_port <= 65535):
        raise InvalidHostsError(f"db_port 범위 오류: {primary_port}")

    result: List[Tuple[str, int]] = [(primary_host, primary_port)]

    for item in additional_hosts or []:
        h = (item.get("host") or "").strip() if isinstance(item, dict) else ""
        if not h or "," in h:
            raise InvalidHostsError(
                f"additional_hosts 의 host 형식 오류: {item!r}"
            )
        try:
            p = int(item.get("port", default_port))
        except (TypeError, ValueError):
            raise InvalidHostsError(f"additional_hosts 의 port 형식 오류: {item!r}")
        if not (1 <= p <= 65535):
            raise InvalidHostsError(f"additional_hosts port 범위 오류: {p}")
        result.append((h, p))

    return result


def validate_db_type_supports_multi_host(db_type: str, hosts: List[Tuple[str, int]]) -> None:
    """multi-host 미지원 db_type 에 추가 host 지정 시 reject."""
    db_type_norm = (db_type or "").lower()
    if len(hosts) > 1 and db_type_norm not in MULTI_HOST_SUPPORTED_TYPES:
        raise MultiHostNotSupportedError(
            f"{db_type_norm} 은 multi-host 를 지원하지 않습니다. "
            f"PostgreSQL/Oracle 만 지원."
        )


# ─────────────────────────────────────────────────────────────
# 옵션 helper
# ─────────────────────────────────────────────────────────────

def _is_load_balance(options: Any) -> bool:
    return extract_multi_host_mode(options) == "load_balance"


def _pg_target_session_attrs(options: Any) -> Optional[str]:
    """options.target_session_attrs 추출 (libpq 14+)."""
    opts = parse_options_dict(options)
    val = opts.get("target_session_attrs")
    if val and val in {"any", "read-write", "read-only", "primary", "standby", "prefer-standby"}:
        return val
    return None


# ─────────────────────────────────────────────────────────────
# PostgreSQL — SQLAlchemy URL/connect_args 빌더
# ─────────────────────────────────────────────────────────────

def build_postgres_sqlalchemy(
    hosts: List[Tuple[str, int]],
    db_name: str,
    username: str,
    password: str,
    ssl_mode: Optional[str] = None,
    schema: Optional[str] = None,
    options: Any = None,
) -> Tuple[URL, Dict[str, Any]]:
    """
    PostgreSQL SQLAlchemy URL + connect_args 생성.

    단일 host (len=1):
        URL.create(host=h, port=p, ...)  — 기존 단일 host 경로와 byte-동일

    Multi-host 공통 port (len>1, port 모두 동일):
        URL.create(host="h1,h2,h3", port=공통, ...)
        Risk #1-A 검증

    Multi-host 다른 port (len>1, port 다양):
        URL.create() 의 host/port 비우고 connect_args 로 csv 전달
        Risk #1b-B2 검증

    Multi-host options 부착:
        load_balance → query["load_balance_hosts"] = "random" (libpq 16+)
        target_session_attrs → query["target_session_attrs"] = val (libpq 14+)
    """
    url_query: Dict[str, str] = {}
    if ssl_mode:
        url_query["sslmode"] = ssl_mode

    if len(hosts) > 1:
        if _is_load_balance(options):
            url_query["load_balance_hosts"] = "random"
        tsa = _pg_target_session_attrs(options)
        if tsa:
            url_query["target_session_attrs"] = tsa

    connect_args: Dict[str, Any] = {}
    if schema and schema.strip().lower() != "public":
        connect_args["options"] = f"-csearch_path={schema.strip()}"

    if len(hosts) == 1:
        h, p = hosts[0]
        url = URL.create(
            drivername="postgresql+psycopg",
            username=username,
            password=password,
            host=h,
            port=p,
            database=db_name,
            query=url_query,
        )
        return url, connect_args

    # multi-host 분기
    all_ports_same = len({p for _, p in hosts}) == 1
    host_csv = ",".join(h for h, _ in hosts)

    if all_ports_same:
        url = URL.create(
            drivername="postgresql+psycopg",
            username=username,
            password=password,
            host=host_csv,
            port=hosts[0][1],
            database=db_name,
            query=url_query,
        )
        return url, connect_args

    # 다른 port — URL 의 host/port 비우고 connect_args 로 전달
    port_csv = ",".join(str(p) for _, p in hosts)
    url = URL.create(
        drivername="postgresql+psycopg",
        username=username,
        password=password,
        database=db_name,
        query=url_query,
    )
    connect_args = {
        **connect_args,
        "host": host_csv,
        "port": port_csv,
    }
    return url, connect_args


def build_postgres_libpq_dsn(
    hosts: List[Tuple[str, int]],
    db_name: str,
    username: str,
    password: str,
    ssl_mode: Optional[str] = None,
    options: Any = None,
    connect_timeout: Optional[int] = None,
) -> str:
    """
    libpq keyword=value DSN — psycopg.connect(conninfo) 직접 호출용.

    `_test_postgresql_connection` 같이 raw psycopg.connect 를 쓰는 사이트에서 사용.
    """
    parts: List[str] = []
    parts.append(f"host={','.join(h for h, _ in hosts)}")
    parts.append(f"port={','.join(str(p) for _, p in hosts)}")
    if db_name:
        parts.append(f"dbname={db_name}")
    if username:
        parts.append(f"user={username}")
    if password:
        parts.append(f"password={password}")
    if ssl_mode:
        parts.append(f"sslmode={ssl_mode}")
    if connect_timeout is not None:
        parts.append(f"connect_timeout={int(connect_timeout)}")

    if len(hosts) > 1:
        if _is_load_balance(options):
            parts.append("load_balance_hosts=random")
        tsa = _pg_target_session_attrs(options)
        if tsa:
            parts.append(f"target_session_attrs={tsa}")

    return " ".join(parts)


# ─────────────────────────────────────────────────────────────
# Oracle — SQLAlchemy URL/connect_args 빌더
# ─────────────────────────────────────────────────────────────

def build_oracle_easy_connect(
    hosts: List[Tuple[str, int]],
    service_name: str,
    use_ssl: bool = False,
) -> str:
    """
    Easy Connect Plus dsn — "h1:p1,h2:p2/service"

    Risk #3-D/E 검증: oracledb thin mode 가 port 동일/다양 모두 인식.
    use_ssl=True 면 "tcps://" 접두 (Oracle TCPS — thin mode 지원 구문).
    """
    if not hosts:
        raise InvalidHostsError("hosts 가 비어있음")
    host_csv = ",".join(f"{h}:{p}" for h, p in hosts)
    dsn = f"{host_csv}/{service_name}" if service_name else host_csv
    return f"tcps://{dsn}" if use_ssl else dsn


def build_oracle_description_lb(
    hosts: List[Tuple[str, int]],
    service_name: str,
    load_balance: bool,
    use_ssl: bool = False,
) -> str:
    """
    DESCRIPTION_LIST 풀 dsn — LOAD_BALANCE/FAILOVER 명시 제어.

    LOAD_BALANCE=ON → oracledb thin mode 가 매 connect 마다 ADDRESS 무작위 선택
    FAILOVER=ON     → 첫 ADDRESS 실패 시 다음 ADDRESS 시도

    Risk #3-F/G 검증.
    """
    protocol = "TCPS" if use_ssl else "TCP"
    addresses = "".join(
        f"(ADDRESS=(PROTOCOL={protocol})(HOST={h})(PORT={p}))" for h, p in hosts
    )
    lb = "ON" if load_balance else "OFF"
    return (
        f"(DESCRIPTION="
        f"(ADDRESS_LIST={addresses})"
        f"(CONNECT_DATA=(SERVICE_NAME={service_name}))"
        f"(FAILOVER=ON)(LOAD_BALANCE={lb}))"
    )


def build_oracle_dsn(
    hosts: List[Tuple[str, int]],
    service_name: str,
    options: Any = None,
    use_ssl: bool = False,
) -> str:
    """
    Oracle dsn 통합:
    - 단일 host → Easy Connect "h:p/svc" (회귀 보존)
    - Multi-host failover → Easy Connect Plus "h1:p1,h2:p2/svc"
    - Multi-host load_balance → DESCRIPTION_LIST + LOAD_BALANCE=ON
    - use_ssl=True → TCPS (Easy Connect 는 tcps:// 접두, DESCRIPTION 은 PROTOCOL=TCPS)

    oracledb.connect(dsn=...) 또는 SQLAlchemy connect_args={"dsn": ...} 로 전달.
    """
    if len(hosts) == 1:
        h, p = hosts[0]
        dsn = f"{h}:{p}/{service_name}" if service_name else f"{h}:{p}"
        return f"tcps://{dsn}" if use_ssl else dsn

    if _is_load_balance(options):
        return build_oracle_description_lb(
            hosts, service_name, load_balance=True, use_ssl=use_ssl,
        )

    # failover (기본)
    return build_oracle_easy_connect(hosts, service_name, use_ssl=use_ssl)


def build_oracle_sqlalchemy(
    hosts: List[Tuple[str, int]],
    service_name: str,
    username: str,
    password: str,
    options: Any = None,
    use_ssl: bool = False,
    ssl_mode: Optional[str] = None,
) -> Tuple[URL, Dict[str, Any]]:
    """
    Oracle SQLAlchemy URL + connect_args 생성.

    단일 host (비 SSL):
        URL.create(host=h, port=p, query={"service_name": ...})  — 기존 회귀 보존

    Multi-host 또는 use_ssl=True:
        URL.create() 의 host/port 비우고 dsn 을 connect_args 로 전달
        (TCPS 는 URL 문법으로 표현할 수 없어 dsn 경유가 유일하게 안전)
        Risk #1-G / #3 검증된 안전 패턴

    ssl_mode "verify-ca"/"verify-full" 이면 서버 인증서 DN 검증
    (python-oracledb thin: ssl_server_dn_match=True) 을 켠다.
    """
    ssl_connect_args: Dict[str, Any] = {}
    if use_ssl and (ssl_mode or "").lower() in {"verify-ca", "verify-full"}:
        ssl_connect_args["ssl_server_dn_match"] = True

    if len(hosts) == 1 and not use_ssl:
        h, p = hosts[0]
        url = URL.create(
            drivername="oracle+oracledb",
            username=username,
            password=password,
            host=h,
            port=p,
            database=None,
            query={"service_name": service_name} if service_name else {},
        )
        return url, {}

    dsn = build_oracle_dsn(hosts, service_name, options=options, use_ssl=use_ssl)
    url = URL.create(
        drivername="oracle+oracledb",
        username=username,
        password=password,
    )
    return url, {"dsn": dsn, **ssl_connect_args}


# ─────────────────────────────────────────────────────────────
# MySQL — multi-host 명시 reject
# ─────────────────────────────────────────────────────────────

def build_mysql_sqlalchemy(
    hosts: List[Tuple[str, int]],
    db_name: str,
    username: str,
    password: str,
    use_ssl: bool = False,
    options: Any = None,
) -> Tuple[URL, Dict[str, Any]]:
    """
    MySQL — PyMySQL multi-host 미지원. len(hosts) > 1 이면 raise.
    """
    if len(hosts) > 1:
        raise MultiHostNotSupportedError(
            "MySQL 은 multi-host 를 지원하지 않습니다. "
            "ProxySQL 등 외부 라우터를 사용하세요."
        )
    h, p = hosts[0]
    url_query: Dict[str, str] = {"charset": "utf8mb4"}
    if use_ssl:
        url_query["ssl"] = "true"
    url = URL.create(
        drivername="mysql+pymysql",
        username=username,
        password=password,
        host=h,
        port=p,
        database=db_name or None,
        query=url_query,
    )
    return url, {}


# ─────────────────────────────────────────────────────────────
# Informix — multi-host 명시 reject
# ─────────────────────────────────────────────────────────────

def build_informix_conn_str(
    hosts: List[Tuple[str, int]],
    db_name: str,
    username: str,
    password: str,
    options: Any = None,
) -> str:
    """
    Informix IfxPy 연결 문자열 — multi-host 미지원.

    options.informix_server 가 있으면 SERVER= 부분 부착.
    """
    if len(hosts) > 1:
        raise MultiHostNotSupportedError(
            "Informix 는 multi-host 를 지원하지 않습니다."
        )
    h, p = hosts[0]

    informix_server = ""
    opts = parse_options_dict(options)
    informix_server = opts.get("informix_server") or ""

    parts: List[str] = []
    if informix_server:
        parts.append(f"SERVER={informix_server}")
    parts.extend([
        f"DATABASE={db_name}",
        f"HOST={h}",
        f"SERVICE={p}",
        "PROTOCOL=onsoctcp",
    ])
    if username:
        parts.append(f"UID={username}")
    if password:
        parts.append(f"PWD={password}")
    return ";".join(parts) + ";"


# ─────────────────────────────────────────────────────────────
# 통합 entry point
# ─────────────────────────────────────────────────────────────

def build_sqlalchemy(
    db_type: str,
    db_host: Optional[str],
    db_port: Optional[int],
    db_name: str,
    username: str,
    password: str,
    additional_hosts: Optional[List[Dict[str, Any]]] = None,
    ssl_mode: Optional[str] = None,
    use_ssl: bool = False,
    schema: Optional[str] = None,
    options: Any = None,
) -> Tuple[URL, Dict[str, Any]]:
    """
    db_type 에 맞는 SQLAlchemy URL + connect_args 통합 빌더.

    호출자는 이 함수만 쓰면 모든 db_type / 단일·멀티 host 분기를 위임 가능.
    """
    db_type_norm = (db_type or "postgresql").lower()
    if db_type_norm not in SUPPORTED_DB_TYPES:
        raise UnsupportedDBTypeError(f"지원하지 않는 db_type: {db_type}")

    hosts = parse_hosts(db_host, db_port, additional_hosts, db_type_norm)
    validate_db_type_supports_multi_host(db_type_norm, hosts)

    if db_type_norm == "postgresql":
        return build_postgres_sqlalchemy(
            hosts, db_name, username, password, ssl_mode, schema, options,
        )
    if db_type_norm == "oracle":
        return build_oracle_sqlalchemy(
            hosts, db_name, username, password, options,
            use_ssl=use_ssl or bool(ssl_mode), ssl_mode=ssl_mode,
        )
    if db_type_norm == "mysql":
        return build_mysql_sqlalchemy(hosts, db_name, username, password, use_ssl, options)

    raise UnsupportedDBTypeError(f"SQLAlchemy 미지원 db_type: {db_type_norm}")


def hosts_display(hosts: List[Tuple[str, int]]) -> str:
    """로깅용 host 표시 — 'h1:p1, h2:p2, h3:p3'"""
    return ", ".join(f"{h}:{p}" for h, p in hosts)
