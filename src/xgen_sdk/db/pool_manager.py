"""
psycopg3 기반 데이터베이스 연결 및 커넥션 풀 관리

주요 기능:
- ConnectionPool을 사용한 커넥션 풀 관리
- 자동 idle 커넥션 정리 (max_idle)
- 커넥션 수명 관리 (max_lifetime)
- 죽은 커넥션 자동 감지 및 폐기 (check callback)
- 자동 재연결 (reconnect_timeout)
- 물리적/논리적 idle 상태 처리

psycopg2와의 호환성:
- 기존 DatabaseManager와 동일한 인터페이스 유지
- 기존 코드 변경 없이 교체 가능
"""
import os
import logging
import sqlite3
import threading
import time
import functools
from typing import Optional, Dict, Any, Callable, TypeVar
from contextlib import contextmanager
from zoneinfo import ZoneInfo

logger = logging.getLogger("database-manager-psycopg3")

# Type variable for retry decorator
T = TypeVar('T')

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg import OperationalError, InterfaceError


TIMEZONE = ZoneInfo(os.getenv('TIMEZONE', 'Asia/Seoul'))

# Retry configuration
DEFAULT_MAX_RETRIES = int(os.getenv('DB_MAX_RETRIES', '3'))
DEFAULT_RETRY_DELAY = float(os.getenv('DB_RETRY_DELAY', '1.0'))
DEFAULT_RETRY_BACKOFF = float(os.getenv('DB_RETRY_BACKOFF', '2.0'))


def with_retry(max_retries: int = DEFAULT_MAX_RETRIES,
               delay: float = DEFAULT_RETRY_DELAY,
               backoff: float = DEFAULT_RETRY_BACKOFF,
               exceptions: tuple = None):
    """
    재시도 데코레이터

    연결 관련 예외 발생 시 자동으로 재시도
    exponential backoff 적용

    Args:
        max_retries: 최대 재시도 횟수
        delay: 초기 대기 시간(초)
        backoff: 대기 시간 증가 배수
        exceptions: 재시도할 예외 타입들
    """
    if exceptions is None:
        exceptions = (OperationalError, InterfaceError, ConnectionError, TimeoutError)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs) -> T:
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries + 1):
                try:
                    # 첫 시도가 아니고 PostgreSQL인 경우 풀 상태 확인 및 복구
                    if attempt > 0 and hasattr(self, '_ensure_pool_healthy'):
                        self._ensure_pool_healthy()

                    return func(self, *args, **kwargs)

                except exceptions as e:
                    last_exception = e

                    if attempt < max_retries:
                        self.logger.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                            f"Retrying in {current_delay:.1f}s..."
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff

                        # 재연결 시도
                        if hasattr(self, '_try_recover_connection'):
                            self._try_recover_connection()
                    else:
                        self.logger.error(
                            f"{func.__name__} failed after {max_retries + 1} attempts: {e}"
                        )

            raise last_exception

        return wrapper
    return decorator


class DatabaseManagerPsycopg3:
    """
    psycopg3 기반 데이터베이스 연결 및 커넥션 풀 관리

    커넥션 풀 핵심 파라미터:
    - min_size: 최소 커넥션 수 (기본: 2)
    - max_size: 최대 커넥션 수 (기본: 10)
    - max_idle: 미사용 커넥션 유지 시간 (초, 기본: 300초 = 5분)
    - max_lifetime: 커넥션 최대 수명 (초, 기본: 1800초 = 30분)
    - reconnect_timeout: 재연결 시도 최대 시간 (초, 기본: 300초)
    - timeout: 커넥션 획득 대기 시간 (초, 기본: 30초)

    핵심 기능:
    1. 커넥션 체크: 매 요청 시 check_connection으로 커넥션 유효성 검증
    2. Idle 관리: max_idle 시간 초과 시 자동으로 커넥션 폐기
    3. 수명 관리: max_lifetime 초과 시 커넥션 자동 교체
    4. 자동 재연결: 연결 실패 시 reconnect_timeout 동안 재시도
    """

    # Pool configuration defaults
    DEFAULT_MIN_SIZE = int(os.getenv('DB_POOL_MIN_SIZE', '2'))
    DEFAULT_MAX_SIZE = int(os.getenv('DB_POOL_MAX_SIZE', '10'))
    DEFAULT_MAX_IDLE = float(os.getenv('DB_POOL_MAX_IDLE', '300'))  # 5 minutes
    DEFAULT_MAX_LIFETIME = float(os.getenv('DB_POOL_MAX_LIFETIME', '1800'))  # 30 minutes
    DEFAULT_RECONNECT_TIMEOUT = float(os.getenv('DB_POOL_RECONNECT_TIMEOUT', '300'))  # 5 minutes
    DEFAULT_TIMEOUT = float(os.getenv('DB_POOL_TIMEOUT', '30'))  # 30 seconds

    def __init__(
        self,
        database_config=None,
        min_size: int = None,
        max_size: int = None,
        max_idle: float = None,
        max_lifetime: float = None,
        reconnect_timeout: float = None,
        timeout: float = None
    ):
        """
        DatabaseManager 초기화

        Args:
            database_config: 데이터베이스 설정 객체
            min_size: 최소 풀 크기
            max_size: 최대 풀 크기
            max_idle: 미사용 커넥션 유지 시간(초)
            max_lifetime: 커넥션 최대 수명(초)
            reconnect_timeout: 재연결 시도 최대 시간(초)
            timeout: 커넥션 획득 대기 시간(초)
        """
        self.config = database_config
        self.db_type = None
        self.logger = logger

        # Pool configuration
        self.min_size = min_size or self.DEFAULT_MIN_SIZE
        self.max_size = max_size or self.DEFAULT_MAX_SIZE
        self.max_idle = max_idle or self.DEFAULT_MAX_IDLE
        self.max_lifetime = max_lifetime or self.DEFAULT_MAX_LIFETIME
        self.reconnect_timeout = reconnect_timeout or self.DEFAULT_RECONNECT_TIMEOUT
        self.timeout = timeout or self.DEFAULT_TIMEOUT

        # Pool instance (for PostgreSQL)
        self._pool: Optional[ConnectionPool] = None
        self._pool_lock = threading.Lock()

        # SQLite connection (for SQLite - 단일 커넥션)
        self._sqlite_connection = None
        self._sqlite_lock = threading.Lock()

        # Pool statistics
        self._stats = {
            'connections_created': 0,
            'connections_closed': 0,
            'connections_failed': 0,
            'health_checks_passed': 0,
            'health_checks_failed': 0,
            'reconnect_attempts': 0,
            'auto_recoveries': 0,
            'retry_successes': 0,
        }

        # Recovery state
        self._recovering = False
        self._recovery_lock = threading.Lock()
        self._last_health_check = 0
        self._health_check_interval = float(os.getenv('DB_HEALTH_CHECK_INTERVAL', '30'))

    @property
    def connection(self):
        """
        Legacy 호환성을 위한 connection 속성

        주의: psycopg3에서는 이 속성 대신 get_connection() 컨텍스트 매니저를 사용해야 함
        SQLite의 경우에만 직접 connection 반환
        """
        if self.db_type == "sqlite":
            return self._sqlite_connection

        # PostgreSQL의 경우 경고 로그 출력
        self.logger.warning(
            "Direct connection access is deprecated in psycopg3. "
            "Use 'with db_manager.get_connection() as conn:' instead."
        )
        return None

    def _build_conninfo(self) -> str:
        """PostgreSQL 연결 문자열 생성"""
        host = self.config.POSTGRES_HOST.value
        port = self.config.POSTGRES_PORT.value
        database = self.config.POSTGRES_DB.value
        user = self.config.POSTGRES_USER.value
        password = self.config.POSTGRES_PASSWORD.value

        return f"host={host} port={port} dbname={database} user={user} password={password}"

    def _configure_connection(self, conn: "psycopg.Connection") -> None:
        """
        커넥션 초기 설정 콜백

        새 커넥션이 생성될 때 호출되어 초기 설정 수행
        - 타임존 설정
        - 기타 세션 파라미터 설정

        주의: configure 콜백에서 쿼리 실행 후 반드시 트랜잭션을 닫아야 함
        (공식 문서: "make sure to close an eventual transaction before leaving the function")
        """
        try:
            timezone_str = str(TIMEZONE)
            conn.execute(f"SET timezone = '{timezone_str}'")
            # 트랜잭션 상태 확인 후 정리 (idle 상태로 복원)
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.commit()
            self.logger.debug(f"Connection configured with timezone: {timezone_str}")
            self._stats['connections_created'] += 1
        except Exception as e:
            self.logger.error(f"Failed to configure connection: {e}")
            # 예외 발생 시에도 트랜잭션 정리 시도
            try:
                if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    conn.rollback()
            except Exception:
                pass
            raise

    def _check_connection(self, conn: "psycopg.Connection") -> None:
        """
        커넥션 유효성 체크 콜백

        커넥션이 풀에서 클라이언트로 전달되기 전에 호출됨
        - SELECT 1 실행으로 물리적/논리적 커넥션 상태 확인
        - 실패 시 예외 발생 → 풀이 해당 커넥션 폐기 후 새 커넥션 제공
        """
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            # 트랜잭션이 열려있으면 롤백 (idle 상태로 복원)
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.rollback()
            self._stats['health_checks_passed'] += 1
        except Exception as e:
            self._stats['health_checks_failed'] += 1
            self.logger.warning(f"Connection check failed: {e}")
            raise

    def _reset_connection(self, conn: "psycopg.Connection") -> None:
        """
        커넥션 리셋 콜백

        커넥션이 풀로 반환될 때 호출됨
        - 열린 트랜잭션 롤백
        - 세션 상태 초기화
        """
        try:
            # 열린 트랜잭션이 있으면 롤백
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.rollback()
        except Exception as e:
            self.logger.warning(f"Failed to reset connection: {e}")
            raise

    def _on_reconnect_failed(self, pool) -> None:
        """
        재연결 실패 콜백

        reconnect_timeout 시간 동안 재연결 시도 실패 시 호출
        기본 동작: 경고 로그 출력 (프로그램 종료 없음)
        """
        self._stats['reconnect_attempts'] += 1
        self.logger.error(
            f"Pool '{pool.name}' failed to reconnect after {self.reconnect_timeout} seconds. "
            "Will continue attempting..."
        )

    def determine_database_type(self) -> str:
        """사용할 데이터베이스 타입 결정"""
        if not self.config:
            return "sqlite"

        db_type = self.config.DATABASE_TYPE.value.lower()
        if db_type in ["sqlite", "postgresql"]:
            return db_type

        if db_type == "auto":
            postgres_required_fields = [
                self.config.POSTGRES_HOST.value,
                self.config.POSTGRES_USER.value,
                self.config.POSTGRES_PASSWORD.value
            ]

            if all(field.strip() for field in postgres_required_fields):
                self.logger.info("PostgreSQL configuration detected, using PostgreSQL with psycopg3")
                return "postgresql"
            else:
                self.logger.info("Using SQLite as default database")
                return "sqlite"

        return "sqlite"

    def connect(self) -> bool:
        """
        데이터베이스 연결 (커넥션 풀 초기화)

        PostgreSQL: ConnectionPool 생성 및 초기화
        SQLite: 단일 커넥션 생성
        """
        try:
            self.db_type = self.determine_database_type()

            if self.db_type == "postgresql":
                return self._connect_postgresql_pool()
            elif self.db_type == "sqlite":
                return self._connect_sqlite()

            return False
        except Exception as e:
            self.logger.error(f"Failed to connect to database: {e}")
            self._stats['connections_failed'] += 1
            return False

    def _connect_postgresql_pool(self) -> bool:
        """
        PostgreSQL ConnectionPool 생성 및 초기화

        핵심 파라미터:
        - min_size/max_size: 풀 크기 범위
        - max_idle: 미사용 커넥션 유지 시간
        - max_lifetime: 커넥션 최대 수명
        - check: 커넥션 유효성 검증 콜백
        - configure: 커넥션 초기 설정 콜백
        - reset: 커넥션 반환 시 리셋 콜백
        """
        try:
            with self._pool_lock:
                if self._pool is not None:
                    self.logger.warning("Pool already exists, closing old pool...")
                    try:
                        self._pool.close()
                    except Exception as e:
                        self.logger.warning(f"Error closing old pool: {e}")

                conninfo = self._build_conninfo()

                self._pool = ConnectionPool(
                    conninfo=conninfo,
                    min_size=self.min_size,
                    max_size=self.max_size,
                    max_idle=self.max_idle,
                    max_lifetime=self.max_lifetime,
                    timeout=self.timeout,
                    reconnect_timeout=self.reconnect_timeout,
                    num_workers=3,
                    # 커넥션 콜백 설정
                    configure=self._configure_connection,
                    check=self._check_connection,
                    reset=self._reset_connection,
                    reconnect_failed=self._on_reconnect_failed,
                    # dict row factory로 설정
                    kwargs={"row_factory": dict_row},
                    # 풀 즉시 열기
                    open=True,
                    name="plateerag-db-pool"
                )

                # 풀 준비 대기 (최소 커넥션 수만큼)
                self._pool.wait(timeout=self.timeout)

                self.logger.info(
                    f"PostgreSQL connection pool initialized: "
                    f"min_size={self.min_size}, max_size={self.max_size}, "
                    f"max_idle={self.max_idle}s, max_lifetime={self.max_lifetime}s"
                )
                return True

        except Exception as e:
            self.logger.error(f"Failed to create PostgreSQL connection pool: {e}")
            self._stats['connections_failed'] += 1
            return False

    def _connect_sqlite(self) -> bool:
        """SQLite 연결 (기존 방식 유지)"""
        try:
            sqlite_path = self.config.SQLITE_PATH.value if self.config else "constants/config.db"
            os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)

            self._sqlite_connection = sqlite3.connect(sqlite_path, check_same_thread=False)
            self._sqlite_connection.row_factory = sqlite3.Row
            self.logger.info(f"Successfully connected to SQLite: {sqlite_path}")
            return True
        except Exception as e:
            self.logger.error(f"SQLite connection failed: {e}")
            return False

    def reconnect(self) -> bool:
        """
        데이터베이스 재연결

        PostgreSQL: 풀의 drain() 메서드로 모든 커넥션 교체
        SQLite: 기존 연결 닫고 새로 연결
        """
        try:
            self.logger.info("Attempting database reconnection...")
            self._stats['reconnect_attempts'] += 1

            if self.db_type == "postgresql":
                with self._pool_lock:
                    if self._pool:
                        # drain()은 모든 기존 커넥션을 폐기하고 새 커넥션으로 교체
                        self._pool.drain()
                        self.logger.info("PostgreSQL pool drained and refreshed")
                        return True
                    else:
                        return self._connect_postgresql_pool()

            elif self.db_type == "sqlite":
                with self._sqlite_lock:
                    if self._sqlite_connection:
                        try:
                            self._sqlite_connection.close()
                        except Exception:
                            pass
                        self._sqlite_connection = None
                    return self._connect_sqlite()

            return False
        except Exception as e:
            self.logger.error(f"Failed to reconnect database: {e}")
            return False

    def health_check(self, auto_recover: bool = True) -> bool:
        """
        데이터베이스 연결 상태 확인

        PostgreSQL: 풀에서 커넥션 획득 후 SELECT 1 실행
        SQLite: 직접 SELECT 1 실행

        Args:
            auto_recover: 실패 시 자동 복구 시도 여부
        """
        try:
            if self.db_type == "postgresql":
                # 풀 상태 먼저 확인
                if not self._is_pool_healthy():
                    if auto_recover:
                        self.logger.warning("Pool unhealthy during health check, attempting recovery...")
                        if not self._try_recover_connection():
                            self.logger.error("Pool recovery failed during health check")
                            self._stats['health_checks_failed'] += 1
                            return False
                    else:
                        self.logger.error("No healthy connection pool available")
                        self._stats['health_checks_failed'] += 1
                        return False

                # 풀에서 커넥션 획득하여 테스트
                with self._pool.connection(timeout=5.0) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        result = cur.fetchone()
                        if result is not None:
                            self._stats['health_checks_passed'] += 1
                            self._last_health_check = time.time()
                            return True
                        return False

            elif self.db_type == "sqlite":
                if not self._sqlite_connection:
                    if auto_recover:
                        if not self._connect_sqlite():
                            self.logger.error("SQLite reconnection failed")
                            self._stats['health_checks_failed'] += 1
                            return False
                    else:
                        self.logger.error("No SQLite connection available")
                        self._stats['health_checks_failed'] += 1
                        return False

                cursor = self._sqlite_connection.cursor()
                cursor.execute("SELECT 1")
                result = cursor.fetchone()
                cursor.close()
                if result is not None:
                    self._stats['health_checks_passed'] += 1
                    self._last_health_check = time.time()
                    return True
                return False

            return False
        except Exception as e:
            self.logger.error(f"Health check failed: {e}")
            self._stats['health_checks_failed'] += 1

            # 자동 복구 시도
            if auto_recover:
                self.logger.info("Attempting recovery after health check failure...")
                if self._try_recover_connection():
                    # 복구 후 재검사
                    return self.health_check(auto_recover=False)

            return False

    def _is_pool_healthy(self) -> bool:
        """
        풀이 정상 상태인지 확인

        Returns:
            bool: 풀이 사용 가능한 상태이면 True
        """
        if self.db_type != "postgresql":
            return self._sqlite_connection is not None

        if not self._pool:
            return False

        try:
            # psycopg_pool의 내부 상태 확인
            # closed 속성이 있으면 확인
            if hasattr(self._pool, 'closed') and self._pool.closed:
                return False

            # _closed 속성 확인 (내부 속성)
            if hasattr(self._pool, '_closed') and self._pool._closed:
                return False

            return True
        except Exception as e:
            self.logger.warning(f"Pool health check error: {e}")
            return False

    def _ensure_pool_healthy(self) -> bool:
        """
        풀이 정상 상태인지 확인하고 필요시 복구

        Returns:
            bool: 풀이 정상 상태이면 True
        """
        if self._is_pool_healthy():
            return True

        self.logger.warning("Pool is not healthy, attempting recovery...")
        return self._try_recover_connection()

    def _try_recover_connection(self) -> bool:
        """
        연결 복구 시도

        동시 복구 시도 방지를 위해 락 사용

        Returns:
            bool: 복구 성공 여부
        """
        with self._recovery_lock:
            if self._recovering:
                self.logger.debug("Recovery already in progress, waiting...")
                # 다른 스레드가 복구 중이면 잠시 대기 후 상태 확인
                time.sleep(0.5)
                return self._is_pool_healthy()

            self._recovering = True

        try:
            self.logger.info("Starting connection recovery...")
            self._stats['auto_recoveries'] += 1

            if self.db_type == "postgresql":
                # 기존 풀 정리 시도
                with self._pool_lock:
                    if self._pool:
                        try:
                            # drain으로 기존 연결 정리
                            self._pool.close()
                        except Exception as e:
                            self.logger.warning(f"Error closing old pool during recovery: {e}")
                        finally:
                            self._pool = None

                # 새 풀 생성
                success = self._connect_postgresql_pool()
                if success:
                    self.logger.info("Connection recovery successful")
                    self._stats['retry_successes'] += 1
                return success

            elif self.db_type == "sqlite":
                return self._connect_sqlite()

            return False

        except Exception as e:
            self.logger.error(f"Connection recovery failed: {e}")
            return False
        finally:
            with self._recovery_lock:
                self._recovering = False

    @contextmanager
    def get_connection(self, timeout: float = None, auto_recover: bool = True):
        """
        커넥션 획득 컨텍스트 매니저

        사용 예:
            with db_manager.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM users")

        PostgreSQL: 풀에서 커넥션 획득 및 자동 반환
        SQLite: 락을 통한 스레드 안전 접근

        Args:
            timeout: 커넥션 획득 대기 시간
            auto_recover: 연결 실패 시 자동 복구 시도 여부
        """
        if self.db_type == "postgresql":
            # 풀 상태 확인 및 필요시 복구
            if not self._is_pool_healthy():
                if auto_recover:
                    if not self._try_recover_connection():
                        raise RuntimeError("Connection pool not available and recovery failed")
                else:
                    raise RuntimeError("Connection pool not initialized")

            effective_timeout = timeout or self.timeout

            try:
                with self._pool.connection(timeout=effective_timeout) as conn:
                    yield conn
            except (OperationalError, InterfaceError) as e:
                # 연결 오류 발생 시 복구 시도 후 재시도
                if auto_recover:
                    self.logger.warning(f"Connection error, attempting recovery: {e}")
                    if self._try_recover_connection():
                        with self._pool.connection(timeout=effective_timeout) as conn:
                            yield conn
                    else:
                        raise
                else:
                    raise

        elif self.db_type == "sqlite":
            with self._sqlite_lock:
                if not self._sqlite_connection:
                    if auto_recover:
                        if not self._connect_sqlite():
                            raise RuntimeError("SQLite connection not available and recovery failed")
                    else:
                        raise RuntimeError("SQLite connection not initialized")
                yield self._sqlite_connection

    def disconnect(self):
        """데이터베이스 연결 해제"""
        if self.db_type == "postgresql":
            with self._pool_lock:
                if self._pool:
                    try:
                        self._pool.close()
                        self._stats['connections_closed'] += 1
                        self.logger.info("PostgreSQL connection pool closed")
                    except Exception as e:
                        self.logger.warning(f"Error closing pool: {e}")
                    finally:
                        self._pool = None

        elif self.db_type == "sqlite":
            with self._sqlite_lock:
                if self._sqlite_connection:
                    try:
                        self._sqlite_connection.close()
                        self.logger.info("SQLite connection closed")
                    except Exception as e:
                        self.logger.warning(f"Error closing SQLite connection: {e}")
                    finally:
                        self._sqlite_connection = None

    def get_pool_stats(self) -> Dict[str, Any]:
        """
        풀 상태 통계 반환

        Returns:
            dict: 풀 상태 정보 (크기, 사용 중, 대기 중 등)
        """
        stats = self._stats.copy()

        if self.db_type == "postgresql" and self._pool:
            try:
                pool_stats = self._pool.get_stats()
                stats.update({
                    'pool_name': self._pool.name,
                    'pool_min_size': self.min_size,
                    'pool_max_size': self.max_size,
                    'pool_size': pool_stats.get('pool_size', 0),
                    'pool_available': pool_stats.get('pool_available', 0),
                    'requests_waiting': pool_stats.get('requests_waiting', 0),
                    'connections_num': pool_stats.get('connections_num', 0),
                    'connections_errors': pool_stats.get('connections_errors', 0),
                    'connections_lost': pool_stats.get('connections_lost', 0),
                })
            except Exception as e:
                self.logger.warning(f"Failed to get pool stats: {e}")

        return stats

    def check_and_refresh_pool(self) -> bool:
        """
        풀 상태 확인 및 필요시 리프레시

        모든 커넥션의 유효성을 체크하고 문제 있는 커넥션 교체
        """
        if self.db_type != "postgresql" or not self._pool:
            return True

        try:
            self._pool.check()
            self.logger.debug("Pool connections checked and refreshed if needed")
            return True
        except Exception as e:
            self.logger.error(f"Pool check failed: {e}")
            return False

    # ============================================================
    # Legacy 호환 메서드들 (기존 DatabaseManager 인터페이스 유지)
    # ============================================================

    def _execute_with_retry(self, operation: Callable[[], T], operation_name: str = "operation") -> T:
        """
        재시도 로직이 포함된 실행 래퍼

        Args:
            operation: 실행할 함수
            operation_name: 로깅용 작업 이름

        Returns:
            작업 결과
        """
        last_exception = None
        current_delay = DEFAULT_RETRY_DELAY

        for attempt in range(DEFAULT_MAX_RETRIES + 1):
            try:
                # 첫 시도가 아니면 풀 상태 확인
                if attempt > 0:
                    self._ensure_pool_healthy()

                return operation()

            except Exception as e:
                last_exception = e

                # 재시도 가능한 예외인지 확인
                is_retryable = isinstance(e, (OperationalError, InterfaceError, ConnectionError, TimeoutError))

                # sqlite 관련 연결 오류도 재시도
                if 'database is locked' in str(e).lower():
                    is_retryable = True

                if is_retryable and attempt < DEFAULT_MAX_RETRIES:
                    self.logger.warning(
                        f"{operation_name} failed (attempt {attempt + 1}/{DEFAULT_MAX_RETRIES + 1}): {e}. "
                        f"Retrying in {current_delay:.1f}s..."
                    )
                    time.sleep(current_delay)
                    current_delay *= DEFAULT_RETRY_BACKOFF

                    # 복구 시도
                    self._try_recover_connection()
                else:
                    # 재시도 불가능하거나 최대 재시도 횟수 도달
                    raise

        raise last_exception

    def execute_query(self, query: str, params: tuple = None) -> Optional[list]:
        """쿼리 실행 (기존 인터페이스 호환) - 자동 재시도 포함"""
        def _do_execute():
            if self.db_type == "postgresql":
                with self.get_connection() as conn:
                    with conn.cursor() as cur:
                        if params:
                            cur.execute(query, params)
                        else:
                            cur.execute(query)

                        if query.strip().upper().startswith('SELECT'):
                            result = cur.fetchall()
                            return list(result) if result else []
                        else:
                            conn.commit()
                            return []

            elif self.db_type == "sqlite":
                with self._sqlite_lock:
                    if not self._sqlite_connection:
                        self._connect_sqlite()
                    cursor = self._sqlite_connection.cursor()
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)

                    if query.strip().upper().startswith('SELECT'):
                        result = cursor.fetchall()
                        return [dict(row) for row in result] if result else []
                    else:
                        self._sqlite_connection.commit()
                        return []

            return []

        try:
            return self._execute_with_retry(_do_execute, "execute_query")
        except Exception as e:
            self.logger.error(f"Query execution failed after retries: {e}")
            return None

    def execute_query_one(self, query: str, params: tuple = None) -> Optional[Dict]:
        """쿼리 실행하여 단일 결과 반환"""
        result = self.execute_query(query, params)
        if result and len(result) > 0:
            return result[0]
        return None

    def execute_insert(self, query: str, params: tuple = None) -> Optional[int]:
        """INSERT 쿼리 실행하여 생성된 ID 반환 - 자동 재시도 포함"""
        def _do_insert():
            if self.db_type == "postgresql":
                with self.get_connection() as conn:
                    with conn.cursor() as cur:
                        if params:
                            cur.execute(query, params)
                        else:
                            cur.execute(query)

                        result = cur.fetchone()
                        conn.commit()

                        if result:
                            if isinstance(result, dict):
                                return result.get("id")
                            elif hasattr(result, '__getitem__'):
                                return result[0]
                        return None

            elif self.db_type == "sqlite":
                with self._sqlite_lock:
                    if not self._sqlite_connection:
                        self._connect_sqlite()
                    cursor = self._sqlite_connection.cursor()
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)

                    insert_id = cursor.lastrowid
                    self._sqlite_connection.commit()
                    return insert_id

            return None

        try:
            return self._execute_with_retry(_do_insert, "execute_insert")
        except Exception as e:
            self.logger.error(f"Insert query execution failed after retries: {e}")
            return None

    def execute_update_delete(self, query: str, params: tuple = None) -> Optional[int]:
        """UPDATE/DELETE 쿼리 실행하여 영향받은 행 수 반환 - 자동 재시도 포함"""
        def _do_update_delete():
            if self.db_type == "postgresql":
                with self.get_connection() as conn:
                    with conn.cursor() as cur:
                        if params:
                            cur.execute(query, params)
                        else:
                            cur.execute(query)

                        affected_rows = cur.rowcount
                        conn.commit()
                        return affected_rows

            elif self.db_type == "sqlite":
                with self._sqlite_lock:
                    if not self._sqlite_connection:
                        self._connect_sqlite()
                    cursor = self._sqlite_connection.cursor()
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)

                    affected_rows = cursor.rowcount
                    self._sqlite_connection.commit()
                    return affected_rows

            return None

        try:
            return self._execute_with_retry(_do_update_delete, "execute_update_delete")
        except Exception as e:
            self.logger.error(f"Update/Delete query execution failed after retries: {e}")
            return None

    def table_exists(self, table_name: str) -> bool:
        """테이블 존재 여부 확인"""
        if self.db_type == "postgresql":
            query = """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = %s
                );
            """
            result = self.execute_query(query, (table_name,))
        else:
            query = """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name=?;
            """
            result = self.execute_query(query, (table_name,))

        return bool(result)

    def run_migrations(self, models_registry=None) -> bool:
        """데이터베이스 마이그레이션 실행"""
        try:
            self.logger.info(f"Running migrations for {self.db_type}")

            fixed_migrations = [
                self._migration_001_add_indexes,
                self._migration_002_fix_column_defaults,
                self._migration_003_fix_db_connections_user_id,
            ]

            for migration in fixed_migrations:
                if not migration():
                    self.logger.error(f"Fixed migration failed: {migration.__name__}")
                    return False

            if models_registry:
                if not self._run_schema_migrations(models_registry):
                    self.logger.error("Schema migrations failed")
                    return False

            self.logger.info("All migrations completed successfully")
            return True

        except Exception as e:
            self.logger.error(f"Migration failed: {e}")
            return False

    # ────────────────────────────────────────────────────────
    # 모델 스키마 정의 → PostgreSQL information_schema.data_type 정규화 매핑
    # ────────────────────────────────────────────────────────
    _MODEL_TO_PG_TYPE = {
        "integer":                  "integer",
        "int":                      "integer",
        "serial":                   "integer",
        "bigint":                   "bigint",
        "bigserial":                "bigint",
        "smallint":                 "smallint",
        "float":                    "double precision",
        "double precision":         "double precision",
        "real":                     "real",
        "numeric":                  "numeric",
        "boolean":                  "boolean",
        "bool":                     "boolean",
        "text":                     "text",
        "varchar":                  "character varying",
        "character varying":        "character varying",
        "char":                     "character",
        "character":                "character",
        "json":                     "json",
        "jsonb":                    "jsonb",
        "text[]":                   "ARRAY",
        "integer[]":                "ARRAY",
        "timestamp":                "timestamp without time zone",
        "timestamp with time zone": "timestamp with time zone",
        "timestamp without time zone": "timestamp without time zone",
        "date":                     "date",
        "time":                     "time without time zone",
        "bytea":                    "bytea",
        "uuid":                     "uuid",
    }

    @classmethod
    def _normalize_pg_type(cls, model_column_def: str) -> str | None:
        """
        모델 스키마 정의 문자열에서 기본 PostgreSQL 타입을 추출하고
        information_schema.data_type과 비교 가능한 정규화된 타입 문자열을 반환합니다.

        예:
            "JSONB DEFAULT '{}'::jsonb"  →  "jsonb"
            "VARCHAR(200) NOT NULL"       →  "character varying"
            "INTEGER REFERENCES users(id) ON DELETE SET NULL" → "integer"
            "TEXT[]"                       →  "ARRAY"
            "BOOLEAN DEFAULT FALSE"       →  "boolean"
        """
        raw = model_column_def.strip()
        if not raw:
            return None

        # 1차: 토큰 분리 전에 TEXT[] 같은 배열 타입 먼저 매칭
        lower = raw.lower()
        if "[]" in lower:
            return "ARRAY"

        # "TIMESTAMP WITH TIME ZONE" 같은 multi-word 타입을 먼저 매칭
        for model_type, pg_type in cls._MODEL_TO_PG_TYPE.items():
            if lower.startswith(model_type):
                # 정확히 그 타입 뒤에 공백, 괄호, 또는 문자열 끝이 오는지 확인
                rest = lower[len(model_type):]
                if not rest or rest[0] in (' ', '(', '\t', '\n'):
                    return pg_type

        # 첫 토큰 기반 fallback
        first_token = raw.split('(')[0].split()[0].lower()
        return cls._MODEL_TO_PG_TYPE.get(first_token)

    def _alter_column_type(self, table_name: str, column_name: str, column_def: str, current_pg_type: str) -> bool:
        """기존 컬럼의 타입을 모델 정의에 맞게 변경합니다."""
        try:
            expected_pg_type = self._normalize_pg_type(column_def)
            if not expected_pg_type:
                self.logger.warning(
                    f"Cannot determine target type for {table_name}.{column_name} "
                    f"from definition '{column_def}', skipping type migration"
                )
                return True  # 알 수 없는 타입이면 skip (실패 아님)

            self.logger.info(
                f"Altering column {table_name}.{column_name}: "
                f"'{current_pg_type}' → '{expected_pg_type}' (def: {column_def})"
            )

            # 타입 변환에 사용할 순수 SQL 타입 (DEFAULT/NOT NULL 등 제거)
            # column_def에서 타입 부분만 추출: 첫 토큰(+괄호+배열) ~ DEFAULT/NOT NULL/REFERENCES 직전까지
            type_sql = self._extract_type_sql(column_def)

            # USING 절: 기존 데이터 캐스팅
            using_clause = self._build_using_clause(column_name, current_pg_type, expected_pg_type, type_sql)

            alter_query = (
                f"ALTER TABLE {table_name} "
                f"ALTER COLUMN {column_name} TYPE {type_sql}{using_clause}"
            )

            self.execute_query(alter_query)
            self.logger.info(f"Successfully altered column {table_name}.{column_name} to {type_sql}")

            # DEFAULT 값이 column_def에 있으면 별도로 설정
            default_val = self._extract_default(column_def)
            if default_val is not None:
                default_query = (
                    f"ALTER TABLE {table_name} "
                    f"ALTER COLUMN {column_name} SET DEFAULT {default_val}"
                )
                try:
                    self.execute_query(default_query)
                except Exception as de:
                    self.logger.warning(f"Failed to set default for {table_name}.{column_name}: {de}")

            return True

        except Exception as e:
            self.logger.error(f"Failed to alter column {table_name}.{column_name}: {e}")
            return False

    @staticmethod
    def _extract_type_sql(column_def: str) -> str:
        """
        컬럼 정의에서 순수 SQL 타입 부분만 추출합니다.
        예: "JSONB DEFAULT '{}'::jsonb"   → "JSONB"
            "VARCHAR(200) NOT NULL"        → "VARCHAR(200)"
            "INTEGER REFERENCES users(id)" → "INTEGER"
            "TEXT[]"                        → "TEXT[]"
            "FLOAT DEFAULT 1.0"            → "FLOAT"
        """
        raw = column_def.strip()
        # 종료 키워드들
        stop_keywords = {'DEFAULT', 'NOT', 'NULL', 'REFERENCES', 'CHECK', 'UNIQUE',
                         'PRIMARY', 'CONSTRAINT', 'ON', 'GENERATED'}
        tokens = raw.split()
        type_parts = []
        i = 0
        while i < len(tokens):
            token_upper = tokens[i].upper()
            if token_upper in stop_keywords:
                break
            type_parts.append(tokens[i])
            i += 1
        return ' '.join(type_parts) if type_parts else raw.split()[0]

    @staticmethod
    def _extract_default(column_def: str) -> str | None:
        """컬럼 정의에서 DEFAULT 값을 추출합니다."""
        upper = column_def.upper()
        idx = upper.find('DEFAULT')
        if idx == -1:
            return None
        rest = column_def[idx + len('DEFAULT'):].strip()
        # DEFAULT 값: 다음 키워드(NOT, NULL, REFERENCES, CHECK 등) 전까지
        stop_keywords = {'NOT', 'NULL', 'REFERENCES', 'CHECK', 'UNIQUE', 'PRIMARY', 'CONSTRAINT', 'ON'}
        tokens = rest.split()
        val_parts = []
        for t in tokens:
            if t.upper() in stop_keywords:
                break
            val_parts.append(t)
        return ' '.join(val_parts) if val_parts else None

    @staticmethod
    def _build_using_clause(column_name: str, from_type: str, to_type: str, type_sql: str) -> str:
        """ALTER COLUMN TYPE 시 USING 절을 생성합니다."""
        # 동일 계열이면 USING 불필요
        if from_type == to_type:
            return ""

        # character varying / text → jsonb: 데이터를 jsonb로 캐스팅
        if to_type == "jsonb" and from_type in ("character varying", "text"):
            return (
                f" USING CASE"
                f" WHEN {column_name} IS NULL OR {column_name} = '' THEN '{{}}'::jsonb"
                f" WHEN {column_name}::text ~ '^\\{{' THEN {column_name}::jsonb"
                f" ELSE jsonb_build_object('_default', {column_name}::text)"
                f" END"
            )

        # character varying → integer
        if to_type == "integer" and from_type == "character varying":
            return f" USING {column_name}::INTEGER"

        # character varying → boolean
        if to_type == "boolean" and from_type == "character varying":
            return f" USING ({column_name}::text = 'true')"

        # 일반적인 캐스팅
        return f" USING {column_name}::{type_sql}"

    def _run_schema_migrations(self, models_registry) -> bool:
        """스키마 변경 감지 및 마이그레이션 실행 (컬럼 추가/삭제/타입 변경)"""
        try:
            self.logger.info("Running schema migrations...")

            for model_class in models_registry:
                table_name = model_class().get_table_name()
                instance = model_class()
                expected_schema = instance.get_schema()
                column_names = set(instance.get_column_names())

                current_columns = self._get_table_columns(table_name)

                if not current_columns:
                    self.logger.warning(f"Table {table_name} does not exist or has no columns")
                    continue

                missing_columns = []
                type_mismatch_columns = []

                for column_name, column_def in expected_schema.items():
                    # 제약조건(UNIQUE_, CHECK_)은 컬럼이 아니므로 스킵
                    if column_name not in column_names:
                        continue

                    if column_name not in current_columns:
                        missing_columns.append((column_name, column_def))
                        self.logger.info(f"Found missing column: {column_name} ({column_def})")
                    elif self.db_type == "postgresql":
                        # 기존 컬럼의 타입 불일치 감지
                        current_pg_type = current_columns[column_name]
                        expected_pg_type = self._normalize_pg_type(column_def)
                        if expected_pg_type and current_pg_type != expected_pg_type:
                            type_mismatch_columns.append((column_name, column_def, current_pg_type))
                            self.logger.info(
                                f"Type mismatch: {table_name}.{column_name} "
                                f"DB='{current_pg_type}' vs Model='{expected_pg_type}'"
                            )

                for column_name, column_def in missing_columns:
                    if not self._add_column_to_table(table_name, column_name, column_def):
                        return False

                # 컬럼 타입 변경
                for column_name, column_def, current_pg_type in type_mismatch_columns:
                    if not self._alter_column_type(table_name, column_name, column_def, current_pg_type):
                        self.logger.warning(
                            f"Failed to alter column type {table_name}.{column_name}, continuing..."
                        )

                # 모델 스키마에 없는 불필요 컬럼 감지 및 제거
                base_columns = {'id', 'created_at', 'updated_at'}
                stale_columns = []
                for column_name in current_columns:
                    if column_name not in column_names and column_name not in base_columns:
                        stale_columns.append(column_name)

                for column_name in stale_columns:
                    self.logger.info(f"Found stale column: {column_name} in table {table_name}")
                    if not self._drop_column_from_table(table_name, column_name):
                        self.logger.warning(f"Failed to drop stale column {column_name} from {table_name}, continuing...")

                if not missing_columns and not stale_columns and not type_mismatch_columns:
                    self.logger.info(f"Table {table_name} schema is up to date")

            return True

        except Exception as e:
            self.logger.error(f"Schema migration failed: {e}")
            return False

    def _get_table_columns(self, table_name: str) -> dict:
        """테이블의 현재 컬럼 구조 조회"""
        try:
            if self.db_type == "postgresql":
                query = """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
                """
                result = self.execute_query(query, (table_name,))

                if result:
                    return {row['column_name']: row['data_type'] for row in result}
                return {}

            else:
                query = f"PRAGMA table_info({table_name})"
                result = self.execute_query(query)

                if result:
                    return {row['name']: row['type'] for row in result}
                return {}

        except Exception as e:
            self.logger.error(f"Failed to get table columns for {table_name}: {e}")
            return {}

    def _add_column_to_table(self, table_name: str, column_name: str, column_def: str) -> bool:
        """테이블에 컬럼 추가"""
        try:
            self.logger.info(f"Adding missing column {column_name} to table {table_name}")

            if self.db_type == "postgresql":
                alter_query = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_def}"
            else:
                alter_query = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}"

            # DDL 문은 결과를 반환하지 않으므로, 예외가 발생하지 않으면 성공으로 간주
            self.execute_query(alter_query)
            self.logger.info(f"Successfully added column {column_name} to {table_name}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to add column {column_name} to {table_name}: {e}")
            return False

    def _drop_column_from_table(self, table_name: str, column_name: str) -> bool:
        """테이블에서 불필요 컬럼 제거"""
        try:
            self.logger.info(f"Dropping stale column {column_name} from table {table_name}")

            if self.db_type == "postgresql":
                alter_query = f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS {column_name}"
            else:
                self.logger.warning(f"SQLite does not support DROP COLUMN easily, skipping {column_name}")
                return False

            self.execute_query(alter_query)
            self.logger.info(f"Successfully dropped column {column_name} from {table_name}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to drop column {column_name} from {table_name}: {e}")
            return False

    def _migration_001_add_indexes(self) -> bool:
        """마이그레이션 001: 인덱스 추가"""
        return True

    def _migration_002_fix_column_defaults(self) -> bool:
        """마이그레이션 002: 컬럼 DEFAULT 값 추가 및 NULL 데이터 수정

        vector_db_chunk_edge 테이블의 edge_type, edge_weight 컬럼에
        DEFAULT 값을 추가하고 기존 NULL 값을 업데이트합니다.
        """
        try:
            self.logger.info("Running migration 002: Fix column defaults for vector_db_chunk_edge")

            # 테이블 존재 여부 확인
            if not self.table_exists("vector_db_chunk_edge"):
                self.logger.info("Table vector_db_chunk_edge does not exist, skipping migration")
                return True

            if self.db_type == "postgresql":
                # 1. edge_type 컬럼에 DEFAULT 값 설정
                try:
                    self.execute_query(
                        "ALTER TABLE vector_db_chunk_edge ALTER COLUMN edge_type SET DEFAULT 'indirect'"
                    )
                    self.logger.info("Set default value for edge_type column")
                except Exception as e:
                    self.logger.debug(f"edge_type default already set or error: {e}")

                # 2. edge_weight 컬럼에 DEFAULT 값 설정
                try:
                    self.execute_query(
                        "ALTER TABLE vector_db_chunk_edge ALTER COLUMN edge_weight SET DEFAULT 1.0"
                    )
                    self.logger.info("Set default value for edge_weight column")
                except Exception as e:
                    self.logger.debug(f"edge_weight default already set or error: {e}")

                # 3. 기존 NULL 값 업데이트
                try:
                    affected = self.execute_update_delete(
                        "UPDATE vector_db_chunk_edge SET edge_type = 'indirect' WHERE edge_type IS NULL"
                    )
                    if affected and affected > 0:
                        self.logger.info(f"Updated {affected} rows with NULL edge_type")
                except Exception as e:
                    self.logger.debug(f"No NULL edge_type values or error: {e}")

                try:
                    affected = self.execute_update_delete(
                        "UPDATE vector_db_chunk_edge SET edge_weight = 1.0 WHERE edge_weight IS NULL"
                    )
                    if affected and affected > 0:
                        self.logger.info(f"Updated {affected} rows with NULL edge_weight")
                except Exception as e:
                    self.logger.debug(f"No NULL edge_weight values or error: {e}")

                # 4. NOT NULL 제약 조건이 없으면 추가 (이미 있으면 무시)
                # PostgreSQL에서는 ALTER COLUMN ... SET NOT NULL 사용
                # 단, 기존 NULL 값이 있으면 실패하므로 위에서 먼저 업데이트

            self.logger.info("Migration 002 completed successfully")
            return True

        except Exception as e:
            self.logger.error(f"Migration 002 failed: {e}")
            return False

    def _migration_003_fix_db_connections_user_id(self) -> bool:
        """마이그레이션 003: db_connections 테이블 user_id 컬럼 타입 수정

        user_id가 VARCHAR(50)으로 잘못 정의되어 있을 경우
        INTEGER REFERENCES users(id)로 변경합니다.
        VARCHAR와 INTEGER 간 타입 불일치로 JOIN/WHERE 조건이 실패하는 문제를 해결합니다.
        """
        try:
            self.logger.info("Running migration 003: Fix db_connections.user_id column type")

            if not self.table_exists("db_connections"):
                self.logger.info("Table db_connections does not exist, skipping migration")
                return True

            if self.db_type == "postgresql":
                # 현재 user_id 컬럼 타입 확인
                result = self.execute_query(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'db_connections' AND column_name = 'user_id'",
                )
                if not result:
                    self.logger.info("user_id column not found in db_connections, skipping")
                    return True

                current_type = result[0].get('data_type', '')
                if current_type == 'integer':
                    self.logger.info("db_connections.user_id is already INTEGER, skipping migration")
                    return True

                self.logger.info(f"db_connections.user_id is '{current_type}', converting to INTEGER...")

                # 1. NOT NULL 제약 조건 제거 (ALTER TYPE 시 방해될 수 있음)
                try:
                    self.execute_query(
                        "ALTER TABLE db_connections ALTER COLUMN user_id DROP NOT NULL"
                    )
                except Exception:
                    pass

                # 2. 기존 외래 키 제약 조건 제거 (있을 경우)
                try:
                    self.execute_query(
                        "ALTER TABLE db_connections DROP CONSTRAINT IF EXISTS fk_db_connections_user_id"
                    )
                except Exception:
                    pass

                # 3. 컬럼 타입 변경 (기존 문자열 값을 INTEGER로 캐스팅)
                try:
                    self.execute_query(
                        "ALTER TABLE db_connections "
                        "ALTER COLUMN user_id TYPE INTEGER USING user_id::INTEGER"
                    )
                    self.logger.info("Changed user_id column type to INTEGER")
                except Exception as e:
                    self.logger.error(f"Failed to alter user_id type: {e}")
                    # 캐스팅 실패 시 (비숫자 데이터 존재) 기존 데이터 삭제 후 재시도
                    try:
                        self.execute_update_delete(
                            "DELETE FROM db_connections WHERE user_id !~ '^[0-9]+$'"
                        )
                        self.execute_query(
                            "ALTER TABLE db_connections "
                            "ALTER COLUMN user_id TYPE INTEGER USING user_id::INTEGER"
                        )
                        self.logger.info("Changed user_id column type to INTEGER (after cleanup)")
                    except Exception as e2:
                        self.logger.error(f"Failed to alter user_id type even after cleanup: {e2}")
                        return False

                # 4. 외래 키 제약 조건 추가
                try:
                    self.execute_query(
                        "ALTER TABLE db_connections "
                        "ADD CONSTRAINT fk_db_connections_user_id "
                        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL"
                    )
                    self.logger.info("Added foreign key constraint for user_id")
                except Exception as e:
                    self.logger.warning(f"Foreign key constraint already exists or failed: {e}")

            self.logger.info("Migration 003 completed successfully")
            return True

        except Exception as e:
            self.logger.error(f"Migration 003 failed: {e}")
            return False


# 싱글톤 패턴
_db_manager_psycopg3 = None
_db_manager_lock = threading.Lock()


def get_database_manager_psycopg3(database_config=None) -> DatabaseManagerPsycopg3:
    """데이터베이스 매니저 싱글톤 인스턴스 반환"""
    global _db_manager_psycopg3

    with _db_manager_lock:
        if _db_manager_psycopg3 is None or database_config is not None:
            _db_manager_psycopg3 = DatabaseManagerPsycopg3(database_config)
        return _db_manager_psycopg3


def reset_database_manager_psycopg3():
    """데이터베이스 매니저 싱글톤 리셋"""
    global _db_manager_psycopg3

    with _db_manager_lock:
        if _db_manager_psycopg3:
            _db_manager_psycopg3.disconnect()
        _db_manager_psycopg3 = None


def initialize_database_psycopg3(database_config=None) -> bool:
    """데이터베이스 초기화 및 마이그레이션"""
    db_manager = get_database_manager_psycopg3(database_config)

    if not db_manager.connect():
        return False

    if database_config and database_config.AUTO_MIGRATION.value:
        return db_manager.run_migrations()

    return True
