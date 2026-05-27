"""
xgen_sdk.db_connections — 외부 DB 연결 (multi-host 지원) URL/DSN 통합 빌더.

사용자가 등록한 외부 DB connection (PostgreSQL/Oracle/MySQL/Informix) 의
URL/DSN 조립을 단일 진실로 통합. multi-host failover/load-balance 지원.

xgen-core (engine_factory, admin connection controller) + xgen-workflow
(database_loader, database_query_reader, dbConnection controllers) 양쪽이
같은 모듈을 import 해서 사용 → 분산 사이트 회귀 방지.

NOTE: 이 모듈은 xgen-sdk 에 위치 — 두 서비스 모두에 설치되는 공통 패키지.
이전엔 xgen-core 의 service.db_connections.dsn_builder 였으나 xgen-workflow
컨테이너에 그 모듈이 존재하지 않아 ImportError 발생 (회귀 사고). 본 모듈로 이전.

Usage:
    from xgen_sdk.db_connections import (
        parse_hosts,
        extract_additional_hosts,
        extract_multi_host_mode,
        hosts_display,
        build_sqlalchemy,
        build_postgres_sqlalchemy,
        build_postgres_libpq_dsn,
        build_oracle_sqlalchemy,
        build_oracle_dsn,
        build_oracle_easy_connect,
        build_oracle_description_lb,
        build_mysql_sqlalchemy,
        build_informix_conn_str,
        UnsupportedDBTypeError,
        MultiHostNotSupportedError,
        InvalidHostsError,
        MULTI_HOST_SUPPORTED_TYPES,
        VALID_MULTI_HOST_MODES,
        DEFAULT_PORTS,
    )
"""
from xgen_sdk.db_connections.dsn_builder import (
    DEFAULT_PORTS,
    InvalidHostsError,
    MULTI_HOST_SUPPORTED_TYPES,
    MultiHostNotSupportedError,
    SUPPORTED_DB_TYPES,
    UnsupportedDBTypeError,
    VALID_MULTI_HOST_MODES,
    build_informix_conn_str,
    build_mysql_sqlalchemy,
    build_oracle_description_lb,
    build_oracle_dsn,
    build_oracle_easy_connect,
    build_oracle_sqlalchemy,
    build_postgres_libpq_dsn,
    build_postgres_sqlalchemy,
    build_sqlalchemy,
    extract_additional_hosts,
    extract_multi_host_mode,
    hosts_display,
    parse_hosts,
    parse_options_dict,
    validate_db_type_supports_multi_host,
)

__all__ = [
    # constants
    "DEFAULT_PORTS",
    "MULTI_HOST_SUPPORTED_TYPES",
    "SUPPORTED_DB_TYPES",
    "VALID_MULTI_HOST_MODES",
    # exceptions
    "InvalidHostsError",
    "MultiHostNotSupportedError",
    "UnsupportedDBTypeError",
    # parsing
    "extract_additional_hosts",
    "extract_multi_host_mode",
    "parse_hosts",
    "parse_options_dict",
    "validate_db_type_supports_multi_host",
    # builders
    "build_informix_conn_str",
    "build_mysql_sqlalchemy",
    "build_oracle_description_lb",
    "build_oracle_dsn",
    "build_oracle_easy_connect",
    "build_oracle_sqlalchemy",
    "build_postgres_libpq_dsn",
    "build_postgres_sqlalchemy",
    "build_sqlalchemy",
    # utils
    "hosts_display",
]
