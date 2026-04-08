"""
Database Configuration Module

환경변수 기반의 PostgreSQL 데이터베이스 설정을 관리합니다.
다른 모듈에서 database_config를 import하여 재사용할 수 있습니다.

로컬 개발: xgen-core 프로젝트 루트에 `.env` 파일을 두면 시작 시 자동 로드됩니다.
(`python-dotenv` — Docker/K8s 등에서 이미 주입된 환경변수는 덮어쓰지 않음)

Usage:
    from service.database.database_config import database_config

    host = database_config.POSTGRES_HOST.value
    port = database_config.POSTGRES_PORT.value
"""

import os
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        pass

# 호출 컨테이너의 프로젝트 루트에서 .env를 탐색
# (예: xgen-core/.env, xgen-workflow/.env 등)
_PROJECT_ROOT = Path.cwd()

class ConfigValue:
    """설정 값을 .value 속성으로 접근할 수 있게 하는 래퍼 클래스"""

    def __init__(self, value: Any):
        self._value = value

    @property
    def value(self) -> Any:
        return self._value

    def __repr__(self) -> str:
        return f"ConfigValue({self._value!r})"

    def __str__(self) -> str:
        return str(self._value)


class DatabaseConfig:
    """
    PostgreSQL 데이터베이스 설정 클래스

    환경변수에서 설정을 읽어오며, 기본값을 제공합니다.
    각 설정은 .value 속성을 통해 접근할 수 있습니다.

    Environment Variables:
        - POSTGRES_HOST: PostgreSQL 호스트 (기본값: postgres — Docker 서비스명; 로컬은 `localhost`로 .env 설정)
        - POSTGRES_PORT: PostgreSQL 포트 (기본값: 5432)
        - POSTGRES_DB: 데이터베이스 이름 (기본값: plateerag)
        - POSTGRES_USER: 데이터베이스 사용자 (기본값: ailab)
        - POSTGRES_PASSWORD: 데이터베이스 비밀번호 (기본값: ailab123)
        - AUTO_MIGRATION: 자동 마이그레이션 여부 (기본값: true)
    """

    def __init__(self):
        self._load_config()

    def _load_config(self) -> None:
        """환경변수에서 설정을 로드합니다."""
        load_dotenv(_PROJECT_ROOT / ".env", override=False)
        self.POSTGRES_HOST = ConfigValue(
            os.getenv("POSTGRES_HOST", "postgres")
        )
        self.POSTGRES_PORT = ConfigValue(
            os.getenv("POSTGRES_PORT", "5432")
        )
        self.POSTGRES_DB = ConfigValue(
            os.getenv("POSTGRES_DB", "plateerag")
        )
        self.POSTGRES_USER = ConfigValue(
            os.getenv("POSTGRES_USER", "ailab")
        )
        self.POSTGRES_PASSWORD = ConfigValue(
            os.getenv("POSTGRES_PASSWORD", "ailab123")
        )
        # PostgreSQL 고정
        self.DATABASE_TYPE = ConfigValue("postgresql")
        # AUTO_MIGRATION: true, 1, yes, on 중 하나면 True
        self.AUTO_MIGRATION = ConfigValue(
            os.getenv("AUTO_MIGRATION", "true").lower() in ('true', '1', 'yes', 'on')
        )

    def reload(self) -> None:
        """환경변수에서 설정을 다시 로드합니다."""
        self._load_config()

    def get_connection_string(self) -> str:
        """PostgreSQL 연결 문자열을 반환합니다."""
        return (
            f"postgresql://{self.POSTGRES_USER.value}:{self.POSTGRES_PASSWORD.value}"
            f"@{self.POSTGRES_HOST.value}:{self.POSTGRES_PORT.value}/{self.POSTGRES_DB.value}"
        )

    def get_connection_dict(self) -> dict:
        """PostgreSQL 연결 정보를 딕셔너리로 반환합니다."""
        return {
            "host": self.POSTGRES_HOST.value,
            "port": self.POSTGRES_PORT.value,
            "database": self.POSTGRES_DB.value,
            "user": self.POSTGRES_USER.value,
            "password": self.POSTGRES_PASSWORD.value,
        }

    def __repr__(self) -> str:
        return (
            f"DatabaseConfig("
            f"host={self.POSTGRES_HOST.value}, "
            f"port={self.POSTGRES_PORT.value}, "
            f"db={self.POSTGRES_DB.value}, "
            f"user={self.POSTGRES_USER.value}, "
            f"type={self.DATABASE_TYPE.value}, "
            f"auto_migration={self.AUTO_MIGRATION.value})"
        )


# 싱글톤 인스턴스 - 다른 모듈에서 import하여 사용
database_config = DatabaseConfig()


# 편의를 위한 함수들
def get_database_config() -> DatabaseConfig:
    """데이터베이스 설정 인스턴스를 반환합니다."""
    return database_config


def reload_database_config() -> DatabaseConfig:
    """환경변수에서 설정을 다시 로드하고 인스턴스를 반환합니다."""
    database_config.reload()
    return database_config
