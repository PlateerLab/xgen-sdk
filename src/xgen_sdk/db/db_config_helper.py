"""
DB Config Helper

DB에서 설정을 읽고 쓰는 간단한 헬퍼 함수들
psycopg3 기반 DatabaseManagerPsycopg3 및 AppDatabaseManager와 호환

주요 변경 (psycopg3):
- connection 속성 대신 get_connection() 컨텍스트 매니저 사용
- AppDatabaseManager 및 DatabaseManagerPsycopg3 모두 지원
"""
import logging
from typing import Any, Optional, Dict, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from xgen_sdk.db.pool_manager import DatabaseManagerPsycopg3
    from xgen_sdk.db.app_db import XgenDB

logger = logging.getLogger(__name__)


def _get_db_manager(db_manager) -> Optional["DatabaseManagerPsycopg3"]:
    """
    db_manager에서 실제 DatabaseManagerPsycopg3 인스턴스 추출

    Args:
        db_manager: AppDatabaseManager 또는 DatabaseManagerPsycopg3 인스턴스

    Returns:
        DatabaseManagerPsycopg3 인스턴스 또는 None
    """
    if db_manager is None:
        return None

    # AppDatabaseManager의 경우 내부 config_db_manager 사용
    if hasattr(db_manager, 'config_db_manager'):
        return db_manager.config_db_manager

    # 이미 DatabaseManagerPsycopg3인 경우
    return db_manager


def _is_db_available(db_manager) -> bool:
    """
    DB 연결이 사용 가능한지 확인 (psycopg3 호환)

    psycopg3에서는 connection 속성 대신 풀 상태를 확인

    Args:
        db_manager: DB 매니저 인스턴스

    Returns:
        DB 사용 가능 여부
    """
    actual_manager = _get_db_manager(db_manager)

    if actual_manager is None:
        return False

    # psycopg3: 풀 상태 또는 SQLite 연결 확인
    if hasattr(actual_manager, '_is_pool_healthy'):
        return actual_manager._is_pool_healthy()
    elif hasattr(actual_manager, 'db_type') and actual_manager.db_type == 'sqlite':
        return actual_manager._sqlite_connection is not None
    elif hasattr(actual_manager, 'connection') and actual_manager.connection:
        # 레거시 호환성
        return True

    return False


def get_db_config(db_manager, config_path: str) -> Optional[Any]:
    """
    DB에서 설정 값 조회 (psycopg3 호환)

    Args:
        db_manager: DatabaseManagerPsycopg3 또는 AppDatabaseManager 인스턴스
        config_path: 설정 경로 (예: "openai.api_key")

    Returns:
        설정 값 또는 None
    """
    actual_manager = _get_db_manager(db_manager)

    if not _is_db_available(db_manager):
        return None

    try:
        # persistent_configs 테이블에서 조회
        db_type = getattr(actual_manager, 'db_type', 'postgresql')

        if db_type == "postgresql":
            query = """
                SELECT config_value, data_type, env_name
                FROM persistent_configs
                WHERE config_path = %s
                LIMIT 1
            """
        else:  # sqlite
            query = """
                SELECT config_value, data_type, env_name
                FROM persistent_configs
                WHERE config_path = ?
                LIMIT 1
            """

        result = actual_manager.execute_query_one(query, (config_path,))

        if result:
            value = result.get('config_value')
            data_type = result.get('data_type', 'string')

            # 타입 변환
            return _convert_db_value(value, data_type)

        return None

    except Exception as e:
        logger.debug(f"Failed to get config from DB: {config_path} - {e}")
        return None


def set_db_config(db_manager, config_path: str,
                  config_value: Any, config_type: str = "string",
                  env_name: Optional[str] = None) -> bool:
    """
    DB에 설정 값 저장 (INSERT or UPDATE) - psycopg3 호환

    Args:
        db_manager: DatabaseManagerPsycopg3 또는 AppDatabaseManager 인스턴스
        config_path: 설정 경로
        config_value: 설정 값
        config_type: 데이터 타입
        env_name: 환경 변수 이름

    Returns:
        성공 여부
    """
    actual_manager = _get_db_manager(db_manager)

    if not _is_db_available(db_manager):
        return False

    try:
        from xgen_sdk.db.config_serializer import safe_serialize

        # safe_serialize 사용 - 이중 직렬화 방지
        value_str = safe_serialize(config_value, config_type)

        env_name = env_name or config_path  # env_name이 없으면 config_path 사용

        db_type = getattr(actual_manager, 'db_type', 'postgresql')

        if db_type == "postgresql":
            # UPSERT 쿼리 (PostgreSQL)
            query = """
                INSERT INTO persistent_configs (config_path, config_value, data_type, env_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(config_path) DO UPDATE SET
                    config_value = EXCLUDED.config_value,
                    data_type = EXCLUDED.data_type,
                    env_name = EXCLUDED.env_name,
                    updated_at = CURRENT_TIMESTAMP
            """
        else:  # sqlite
            # UPSERT 쿼리 (SQLite)
            query = """
                INSERT INTO persistent_configs (config_path, config_value, data_type, env_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(config_path) DO UPDATE SET
                    config_value = excluded.config_value,
                    data_type = excluded.data_type,
                    env_name = excluded.env_name,
                    updated_at = CURRENT_TIMESTAMP
            """

        actual_manager.execute_query(query, (config_path, value_str, config_type, env_name))
        logger.debug(f"Saved config to DB: {config_path} (env_name: {env_name}) = {config_value}")
        return True

    except Exception as e:
        logger.error(f"Failed to save config to DB: {config_path} - {e}")
        return False


def get_all_db_configs(db_manager) -> Dict[str, Any]:
    """
    DB에서 모든 설정 조회 (psycopg3 호환)

    Args:
        db_manager: DatabaseManagerPsycopg3 또는 AppDatabaseManager 인스턴스

    Returns:
        {config_path: config_value} 딕셔너리
    """
    actual_manager = _get_db_manager(db_manager)

    if not _is_db_available(db_manager):
        return {}

    try:
        query = "SELECT config_path, config_value, data_type, env_name FROM persistent_configs"
        results = actual_manager.execute_query(query)

        if not results:
            return {}

        configs = {}
        for row in results:
            path = row.get('config_path')
            value = row.get('config_value')
            data_type = row.get('data_type', 'string')

            configs[path] = _convert_db_value(value, data_type)

        return configs

    except Exception as e:
        logger.error(f"Failed to get all configs from DB: {e}")
        return {}


def _convert_db_value(value: str, config_type: str) -> Any:
    """
    DB 문자열 값을 실제 타입으로 변환 (safe_deserialize 사용)

    다중 이스케이프된 JSON 문자열도 안전하게 처리

    Args:
        value: DB에서 읽은 문자열 값
        config_type: 타입 정보

    Returns:
        변환된 값
    """
    from xgen_sdk.db.config_serializer import safe_deserialize
    return safe_deserialize(value, config_type)


def ensure_config_table_exists(db_manager) -> bool:
    """
    persistent_configs 테이블이 존재하는지 확인하고, 없으면 생성 (psycopg3 호환)

    Args:
        db_manager: DatabaseManagerPsycopg3 또는 AppDatabaseManager 인스턴스

    Returns:
        성공 여부
    """
    actual_manager = _get_db_manager(db_manager)

    if not _is_db_available(db_manager):
        return False

    try:
        # 테이블 존재 여부 확인
        if actual_manager.table_exists("persistent_configs"):
            logger.debug("persistent_configs table already exists")
            return True

        # DB 타입에 따라 테이블 생성 쿼리 결정
        db_type = getattr(actual_manager, 'db_type', 'postgresql')

        # persistent_configs 테이블을 직접 생성 (모델 의존 제거)
        if db_type == "postgresql":
            create_query = """
                CREATE TABLE IF NOT EXISTS persistent_configs (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    env_name TEXT NOT NULL,
                    config_path TEXT NOT NULL UNIQUE,
                    config_value TEXT,
                    data_type TEXT DEFAULT 'string',
                    category TEXT
                )
            """
        else:
            create_query = """
                CREATE TABLE IF NOT EXISTS persistent_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    env_name TEXT NOT NULL,
                    config_path TEXT NOT NULL UNIQUE,
                    config_value TEXT,
                    data_type TEXT DEFAULT 'string',
                    category TEXT
                )
            """
        actual_manager.execute_query(create_query)

        logger.info("Created persistent_configs table using PersistentConfigModel")
        return True

    except Exception as e:
        logger.error(f"Failed to ensure config table exists: {e}")
        return False
