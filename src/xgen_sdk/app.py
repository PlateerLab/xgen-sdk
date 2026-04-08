"""
XgenApp — 통합 애플리케이션 부트스트랩 클래스

모든 컨테이너의 main.py에서 개별적으로 초기화하던
DB, Config, Storage 등을 하나의 클래스로 통합합니다.

사용법:
    from xgen_sdk import XgenApp

    # 간단한 초기화 (환경변수에서 자동 설정 로드)
    xgen = XgenApp()
    xgen.boot()

    # FastAPI lifespan에서 사용
    app_db = xgen.db                # XgenDB 인스턴스
    config = xgen.config            # RedisConfigManager 또는 LocalConfigManager
    minio = xgen.minio_client       # Minio 인스턴스

    # 종료 시
    xgen.shutdown()
"""

import logging
from typing import Optional, Any, List, Type

logger = logging.getLogger("xgen-app")


class XgenApp:
    """
    XGen Platform 통합 부트스트랩.

    boot() 한 번으로 DB + Config + Storage 가 모두 초기화됩니다.
    각 컨테이너의 main.py에서는 이 클래스만 import하면 됩니다.
    """

    def __init__(
        self,
        *,
        enable_db: bool = True,
        enable_config: bool = True,
        enable_storage: bool = True,
        db_models: Optional[List[Any]] = None,
        auto_migrate: bool = False,
    ):
        """
        Args:
            enable_db: PostgreSQL 연결 활성화 (기본: True)
            enable_config: Redis/Local 설정 관리 활성화 (기본: True)
            enable_storage: MinIO 클라이언트 활성화 (기본: True)
            db_models: DB 초기화 시 등록할 ORM 모델 클래스 목록
            auto_migrate: True면 boot() 시 run_migrations() 자동 실행
        """
        self._enable_db = enable_db
        self._enable_config = enable_config
        self._enable_storage = enable_storage
        self._db_models = db_models or []
        self._auto_migrate = auto_migrate

        self._db: Optional[Any] = None
        self._config: Optional[Any] = None
        self._minio: Optional[Any] = None
        self._booted = False

    # ================================================================
    # Properties
    # ================================================================

    @property
    def db(self):
        """XgenDB(AppDatabaseManager) 인스턴스. boot() 후 사용."""
        if self._db is None:
            raise RuntimeError("XgenApp.db is not available. Call boot() first or enable_db=True.")
        return self._db

    @property
    def config(self):
        """RedisConfigManager 또는 LocalConfigManager 인스턴스."""
        if self._config is None:
            raise RuntimeError("XgenApp.config is not available. Call boot() first or enable_config=True.")
        return self._config

    @property
    def minio_client(self):
        """MinIO 클라이언트 인스턴스."""
        if self._minio is None:
            raise RuntimeError("XgenApp.minio_client is not available. Call boot() first or enable_storage=True.")
        return self._minio

    @property
    def is_booted(self) -> bool:
        return self._booted

    # ================================================================
    # Boot
    # ================================================================

    def boot(self) -> "XgenApp":
        """
        모든 인프라 서비스를 초기화합니다.

        순서: DB → Config (with DB integration) → Storage
        반환값: self (fluent API)
        """
        if self._booted:
            logger.warning("XgenApp is already booted. Skipping.")
            return self

        logger.info("=" * 60)
        logger.info("🚀 XgenApp boot starting...")
        logger.info("=" * 60)

        if self._enable_db:
            self._boot_db()

        if self._enable_config:
            self._boot_config()

        if self._enable_storage:
            self._boot_storage()

        self._booted = True

        logger.info("=" * 60)
        logger.info("✅ XgenApp boot complete!")
        logger.info("   DB:      %s", "✅" if self._db else "❌ disabled")
        logger.info("   Config:  %s (%s)", "✅" if self._config else "❌ disabled",
                     type(self._config).__name__ if self._config else "-")
        logger.info("   Storage: %s", "✅" if self._minio else "❌ disabled")
        logger.info("=" * 60)

        return self

    def _boot_db(self) -> None:
        """PostgreSQL 연결 + 테이블 생성 + (선택) 마이그레이션"""
        from xgen_sdk.db import XgenDB
        from xgen_sdk.db.database_config import database_config

        logger.info("⚙️  [DB] Initializing PostgreSQL connection...")

        self._db = XgenDB(database_config)

        if self._db_models:
            self._db.register_models(self._db_models)
            success = self._db.initialize_database()
        else:
            # 모델이 없으면 테이블 생성 없이 연결만 수행
            success = self._db.initialize_connection()

        if success:
            logger.info("✅ [DB] PostgreSQL connected%s",
                         " and tables initialized" if self._db_models else "")

            if self._auto_migrate and hasattr(self._db, 'run_migrations'):
                if self._db.run_migrations():
                    logger.info("✅ [DB] Migrations completed")
                else:
                    logger.warning("⚠️  [DB] Migrations failed")
        else:
            logger.error("❌ [DB] PostgreSQL initialization failed")
            self._db = None

    def _boot_config(self) -> None:
        """Redis 설정 관리 초기화 (실패 시 Local fallback)"""
        from xgen_sdk.config import create_config_manager

        logger.info("⚙️  [Config] Initializing config manager...")

        self._config = create_config_manager(
            db_manager=self._db if self._enable_db and self._db else None
        )

        manager_type = type(self._config).__name__
        if manager_type == "RedisConfigManager":
            logger.info("✅ [Config] Redis mode active")
        else:
            logger.info("✅ [Config] Local fallback mode (Redis unavailable)")

    def _boot_storage(self) -> None:
        """MinIO 클라이언트 초기화"""
        try:
            from xgen_sdk.storage.minio_client import get_minio_client
            self._minio = get_minio_client()
            logger.info("✅ [Storage] MinIO client initialized")
        except Exception as e:
            logger.warning("⚠️  [Storage] MinIO initialization failed: %s", e)
            self._minio = None

    # ================================================================
    # Shutdown
    # ================================================================

    def shutdown(self) -> None:
        """모든 인프라 서비스를 정리합니다."""
        logger.info("🛑 XgenApp shutdown starting...")

        if self._db:
            try:
                self._db.close()
                logger.info("✅ [DB] Connection closed")
            except Exception as e:
                logger.warning("⚠️  [DB] Close failed: %s", e)
            self._db = None

        if self._config:
            try:
                if hasattr(self._config, 'close'):
                    self._config.close()
                logger.info("✅ [Config] Manager closed")
            except Exception as e:
                logger.warning("⚠️  [Config] Close failed: %s", e)
            self._config = None

        self._minio = None
        self._booted = False

        logger.info("✅ XgenApp shutdown complete")

    # ================================================================
    # Convenience
    # ================================================================

    def get_config_value(self, name: str, default: Any = None) -> Any:
        """설정값 조회 shortcut"""
        return self._config.get_config_value(name, default) if self._config else default

    def ensure_redis_sync(self) -> None:
        """Redis/Local 동기화 점검"""
        if self._config and hasattr(self._config, 'sync_to_redis'):
            self._config.sync_to_redis()
