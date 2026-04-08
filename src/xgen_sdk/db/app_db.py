"""
psycopg3 기반 AppDatabaseManager

기존 AppDatabaseManager와 동일한 인터페이스를 제공하면서
psycopg3의 ConnectionPool을 사용하여 다음 기능을 제공:
- 커넥션 풀 관리 (min_size, max_size)
- 자동 idle 커넥션 정리 (max_idle)
- 커넥션 수명 관리 (max_lifetime)
- 죽은 커넥션 자동 감지 및 폐기 (check callback)
- 자동 재연결 (reconnect_timeout)

사용법:
    기존 코드에서 import만 변경:

    # 기존:
    # from service.database.connection import AppDatabaseManager

    # 변경:
    from service.database.connection_psycopg3 import AppDatabaseManager

    # 사용법은 동일
    app_db = AppDatabaseManager(database_config)
    app_db.initialize_database()
"""
import logging
import json
import re
import time
from typing import List, Dict, Any, Optional, Type, Callable, TypeVar
from xgen_sdk.db.pool_manager import DatabaseManagerPsycopg3
from xgen_sdk.db.base_model import BaseModel
from xgen_sdk.db.config_serializer import safe_serialize
from pydantic import create_model

logger = logging.getLogger("app-database-psycopg3")

# Type variable for decorators
T = TypeVar('T')


class XgenDB:
    """
    psycopg3 기반 Application Database Manager (xgen-sdk)

    기존 AppDatabaseManager와 100% 호환되는 인터페이스 제공
    내부적으로 psycopg3 ConnectionPool 사용

    주요 개선사항:
    1. 커넥션 풀: 여러 커넥션을 미리 생성하여 성능 향상
    2. 자동 체크: 매 요청 시 커넥션 유효성 검증
    3. Idle 관리: 미사용 커넥션 자동 폐기
    4. 수명 관리: 오래된 커넥션 자동 교체
    5. 자동 재연결: 연결 실패 시 자동 재시도
    """

    def __init__(self, database_config=None):
        """
        초기화

        Args:
            database_config: 데이터베이스 설정 객체
        """
        self.config_db_manager = DatabaseManagerPsycopg3(database_config)
        self.logger = logger
        self._models_registry: List[Type[BaseModel]] = []

        # 복구 및 재시도 설정
        self._max_retries = 3
        self._retry_delay = 1.0
        self._retry_backoff = 2.0
        self._last_health_check = 0
        self._health_check_interval = 30  # 초
        self._auto_recover = True

    def _serialize_value(self, value: Any) -> Any:
        """
        psycopg3에서 처리할 수 없는 타입을 직렬화

        dict와 list 타입은 JSON 문자열로 변환
        (psycopg3는 기본적으로 Python dict를 PostgreSQL JSON으로 자동 변환하지 않음)

        Args:
            value: 원본 값

        Returns:
            직렬화된 값 (dict/list는 JSON 문자열, 나머지는 그대로)
        """
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(value, list):
            # PostgreSQL TEXT[] 배열이 아닌 일반 리스트는 JSON으로 직렬화
            # (문자열 리스트가 아니거나 복잡한 객체를 포함하는 경우)
            if value and not all(isinstance(item, str) for item in value):
                return json.dumps(value, ensure_ascii=False, default=str)
            return value
        return value

    def _serialize_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        데이터 딕셔너리의 모든 값을 직렬화

        Args:
            data: 원본 데이터 딕셔너리

        Returns:
            직렬화된 데이터 딕셔너리
        """
        return {k: self._serialize_value(v) for k, v in data.items()}

    def _ensure_connection(self) -> bool:
        """
        연결 상태 확인 및 필요시 복구

        주기적인 health check를 수행하고
        문제 발견 시 자동 복구 시도

        Returns:
            bool: 연결이 정상이면 True
        """
        current_time = time.time()

        # 주기적 health check (interval 이내면 skip)
        if current_time - self._last_health_check < self._health_check_interval:
            return True

        try:
            if self.check_health():
                self._last_health_check = current_time
                return True
            else:
                # health check 실패 시 재연결 시도
                return self.reconnect()
        except Exception as e:
            self.logger.warning(f"Connection ensure failed: {e}")
            return self.reconnect()

    def _with_auto_recovery(self, operation: Callable[[], T], operation_name: str = "operation") -> T:
        """
        자동 복구가 포함된 작업 실행 래퍼

        Args:
            operation: 실행할 함수
            operation_name: 로깅용 작업 이름

        Returns:
            작업 결과
        """
        last_exception = None
        current_delay = self._retry_delay

        for attempt in range(self._max_retries + 1):
            try:
                # 첫 시도가 아니면 연결 확인
                if attempt > 0:
                    self._ensure_connection()

                return operation()

            except Exception as e:
                last_exception = e
                error_str = str(e).lower()

                # 재시도 가능한 오류인지 확인
                is_retryable = any(keyword in error_str for keyword in [
                    'connection', 'timeout', 'closed', 'refused',
                    'reset', 'broken', 'network', 'operational',
                    'interface', 'pool', 'unavailable'
                ])

                if is_retryable and attempt < self._max_retries:
                    self.logger.warning(
                        f"{operation_name} failed (attempt {attempt + 1}/{self._max_retries + 1}): {e}. "
                        f"Retrying in {current_delay:.1f}s..."
                    )
                    time.sleep(current_delay)
                    current_delay *= self._retry_backoff

                    # 재연결 시도
                    self.reconnect()
                else:
                    raise

        raise last_exception

    def check_health(self) -> bool:
        """
        데이터베이스 연결 상태 확인

        psycopg3 풀에서는 check 콜백이 자동으로 커넥션 유효성을 검증하므로
        health_check()가 성공하면 커넥션이 정상임을 보장
        """
        try:
            return self.config_db_manager.health_check(auto_recover=self._auto_recover)
        except Exception as e:
            self.logger.error("Database health check failed: %s", e)
            return False

    def reconnect(self) -> bool:
        """
        데이터베이스 재연결

        psycopg3에서는 pool.drain()을 호출하거나
        풀을 재생성하여 모든 기존 커넥션을 폐기하고 새 커넥션으로 교체
        """
        try:
            self.logger.info("Attempting database reconnection...")

            if self.config_db_manager.reconnect():
                self.logger.info("Database reconnection successful")
                self._last_health_check = time.time()
                return True
            else:
                self.logger.error("Database reconnection failed")
                return False

        except Exception as e:
            self.logger.error(f"Failed to reconnect database: {e}")
            return False

    def get_pool_stats(self) -> Dict[str, Any]:
        """
        커넥션 풀 상태 통계 반환

        Returns:
            dict: 풀 상태 정보 (크기, 사용 중, 대기 중 등)
        """
        return self.config_db_manager.get_pool_stats()

    def check_and_refresh_pool(self) -> bool:
        """
        풀 상태 확인 및 필요시 리프레시

        모든 커넥션의 유효성을 체크하고 문제 있는 커넥션 교체
        """
        return self.config_db_manager.check_and_refresh_pool()

    def register_model(self, model_class: Type[BaseModel]):
        """모델 클래스를 등록"""
        if model_class not in self._models_registry:
            self._models_registry.append(model_class)
            self.logger.info("Registered model: %s", model_class.__name__)

    def register_models(self, model_classes: List[Type[BaseModel]]):
        """여러 모델 클래스를 한 번에 등록"""
        for model_class in model_classes:
            self.register_model(model_class)

    def initialize_database(self, create_tables: bool = True) -> bool:
        """
        데이터베이스 연결 및 테이블 생성

        psycopg3에서는 ConnectionPool을 초기화하고
        min_size 만큼의 커넥션을 미리 생성

        연결 실패 시 재시도 로직 포함

        Args:
            create_tables: True면 테이블 생성, False면 연결만 수행
        """
        last_error = None

        for attempt in range(self._max_retries + 1):
            try:
                if not self.config_db_manager.connect():
                    raise ConnectionError("Failed to connect to database")

                self.logger.info("Connected to database with connection pool")
                self._last_health_check = time.time()

                if create_tables:
                    return self.create_tables()
                return True

            except Exception as e:
                last_error = e
                if attempt < self._max_retries:
                    delay = self._retry_delay * (self._retry_backoff ** attempt)
                    self.logger.warning(
                        f"Database initialization failed (attempt {attempt + 1}/{self._max_retries + 1}): {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                else:
                    self.logger.error("Failed to initialize application database after %d attempts: %s",
                                    self._max_retries + 1, e)

        return False

    def initialize_connection(self) -> bool:
        """
        데이터베이스 연결만 수행 (테이블 생성 없이)

        테이블 생성/마이그레이션이 별도 컨테이너에서 처리되는 경우 사용
        연결 실패 시 재시도 로직 포함

        Returns:
            bool: 연결 성공 여부
        """
        return self.initialize_database(create_tables=False)

    def create_tables(self) -> bool:
        """등록된 모든 모델의 테이블 생성"""
        try:
            db_type = self.config_db_manager.db_type

            for model_class in self._models_registry:
                instance = model_class()
                table_name = instance.get_table_name()
                create_query = model_class.get_create_table_query(db_type)

                self.logger.info("Creating table: %s", table_name)
                self.config_db_manager.execute_query(create_query)

                # 모델에 정의된 인덱스 자동 생성
                for idx_name, columns in instance.get_indexes():
                    try:
                        idx_query = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name}({columns})"
                        self.config_db_manager.execute_query(idx_query)
                        self.logger.info("Created index %s for table: %s", idx_name, table_name)
                    except Exception as e:
                        self.logger.warning("Failed to create index %s for %s: %s", idx_name, table_name, e)

                if hasattr(model_class, '__name__') and model_class.__name__ == 'PersistentConfigModel':
                    index_query = "CREATE INDEX IF NOT EXISTS idx_config_path ON persistent_configs(config_path)"
                    try:
                        self.config_db_manager.execute_query(index_query)
                        self.logger.info("Created index for table: %s", table_name)
                    except Exception as e:
                        self.logger.warning("Failed to create index for %s: %s", table_name, e)

            self.logger.info("All application tables created successfully")
            return True

        except Exception as e:
            self.logger.error("Failed to create application tables: %s", e)
            return False

    def insert(self, model: BaseModel) -> Optional[int]:
        """모델 인스턴스를 데이터베이스에 삽입 - 자동 복구 포함"""
        def _do_insert():
            db_type = self.config_db_manager.db_type
            query, values = model.get_insert_query(db_type)

            # PostgreSQL 배열 타입 처리
            if db_type == "postgresql":
                # 모델의 원본 데이터에서 리스트 타입 필드 확인
                original_data = {}
                for attr_name in dir(model):
                    if not attr_name.startswith('_') and not callable(getattr(model, attr_name)):
                        original_data[attr_name] = getattr(model, attr_name)

                # 쿼리에서 컬럼 목록 추출
                columns_part = query.split('(')[1].split(')')[0]
                columns = [col.strip() for col in columns_part.split(',')]

                # 리스트 타입 필드는 JSON 문자열이 아닌 Python 리스트로 전달
                processed_values = []
                for i, col in enumerate(columns):
                    original_value = original_data.get(col)
                    if isinstance(original_value, list):
                        processed_values.append(original_value if original_value else [])
                    else:
                        processed_values.append(values[i])

                values = processed_values

            insert_id = None
            if db_type == "postgresql":
                query += " RETURNING id"
                insert_id = self.config_db_manager.execute_insert(query, tuple(values))
            else:
                insert_id = self.config_db_manager.execute_insert(query, tuple(values))

            return {"result": "success", "id": insert_id}

        try:
            return self._with_auto_recovery(_do_insert, "insert")
        except Exception as e:
            self.logger.error("Failed to insert %s: %s", model.__class__.__name__, e)
            return None

    def update(self, model: BaseModel) -> bool:
        """모델 인스턴스를 데이터베이스에서 업데이트 - 자동 복구 포함"""
        def _do_update():
            db_type = self.config_db_manager.db_type

            original_data = {}
            for attr_name in dir(model):
                if not attr_name.startswith('_') and not callable(getattr(model, attr_name)):
                    original_data[attr_name] = getattr(model, attr_name)

            query, values = model.get_update_query(db_type)
            set_part = query.split('SET')[1].split('WHERE')[0].strip()
            set_clauses = [clause.strip() for clause in set_part.split(',')]

            processed_values = []
            value_index = 0

            for clause in set_clauses:
                column_name = clause.split('=')[0].strip()
                original_value = original_data.get(column_name)

                if isinstance(original_value, list) and db_type == "postgresql":
                    if original_value:
                        processed_values.append(original_value)
                    else:
                        processed_values.append([])
                else:
                    processed_values.append(values[value_index])

                value_index += 1

            processed_values.append(values[-1])
            self.config_db_manager.execute_update_delete(query, tuple(processed_values))
            return {"result": "success"}

        try:
            return self._with_auto_recovery(_do_update, "update")
        except Exception as e:
            self.logger.error("Failed to update %s: %s", model.__class__.__name__, e)
            return False

    def update_config(self, env_name: str, config_path: str, config_value: Any,
                     data_type: str = "string", category: str = None) -> bool:
        """설정 값 업데이트 (safe_serialize 사용으로 이중 직렬화 방지)"""
        try:
            db_type = self.config_db_manager.db_type
            table_name = "persistent_configs"

            if db_type == "postgresql":
                check_query = f"SELECT id FROM {table_name} WHERE env_name = %s"
            else:
                check_query = f"SELECT id FROM {table_name} WHERE env_name = ?"

            existing = self.config_db_manager.execute_query_one(check_query, (env_name,))

            # safe_serialize 사용 - 이미 JSON 문자열인 경우 재직렬화 방지
            value_str = safe_serialize(config_value, data_type)

            if existing:
                if db_type == "postgresql":
                    update_query = f"""
                        UPDATE {table_name}
                        SET config_path = %s,
                            config_value = %s,
                            data_type = %s,
                            category = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE env_name = %s
                    """
                else:
                    update_query = f"""
                        UPDATE {table_name}
                        SET config_path = ?,
                            config_value = ?,
                            data_type = ?,
                            category = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE env_name = ?
                    """

                affected_rows = self.config_db_manager.execute_update_delete(
                    update_query,
                    (config_path, value_str, data_type, category, env_name)
                )

                return affected_rows is not None and affected_rows > 0
            else:
                if db_type == "postgresql":
                    insert_query = f"""
                        INSERT INTO {table_name} (env_name, config_path, config_value, data_type, category)
                        VALUES (%s, %s, %s, %s, %s)
                    """
                else:
                    insert_query = f"""
                        INSERT INTO {table_name} (env_name, config_path, config_value, data_type, category)
                        VALUES (?, ?, ?, ?, ?)
                    """

                insert_id = self.config_db_manager.execute_insert(
                    insert_query + (" RETURNING id" if db_type == "postgresql" else ""),
                    (env_name, config_path, value_str, data_type, category)
                )

                return insert_id is not None

        except Exception as e:
            self.logger.error("Failed to update config in DB: %s - %s", env_name, e)
            return False

    def delete(self, model_class: Type[BaseModel], record_id: int) -> bool:
        """ID로 레코드 삭제"""
        try:
            table_name = model_class().get_table_name()
            db_type = self.config_db_manager.db_type

            if db_type == "postgresql":
                query = f"DELETE FROM {table_name} WHERE id = %s"
            else:
                query = f"DELETE FROM {table_name} WHERE id = ?"

            affected_rows = self.config_db_manager.execute_update_delete(query, (record_id,))
            return affected_rows is not None and affected_rows > 0

        except Exception as e:
            self.logger.error("Failed to delete %s with id %s: %s",
                            model_class.__name__, record_id, e)
            return False

    def delete_by_condition(self, model_class: Type[BaseModel], conditions: Dict[str, Any]) -> bool:
        """조건으로 레코드 삭제"""
        try:
            table_name = model_class().get_table_name()
            db_type = self.config_db_manager.db_type

            where_clauses = []
            values = []

            for key, value in conditions.items():
                if key.endswith("__like__"):
                    actual_key = key[:-8]
                    if db_type == "postgresql":
                        where_clauses.append(f"{actual_key} LIKE %s")
                    else:
                        where_clauses.append(f"{actual_key} LIKE ?")
                    values.append(f"%{value}%")
                elif key.endswith("__not__"):
                    actual_key = key[:-7]
                    if db_type == "postgresql":
                        where_clauses.append(f"{actual_key} != %s")
                    else:
                        where_clauses.append(f"{actual_key} != ?")
                    values.append(value)
                else:
                    if db_type == "postgresql":
                        where_clauses.append(f"{key} = %s")
                    else:
                        where_clauses.append(f"{key} = ?")
                    values.append(value)

            if not conditions:
                self.logger.warning("No conditions provided for delete_by_condition. Aborting.")
                return False

            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
            query = f"DELETE FROM {table_name} WHERE {where_clause}"

            affected_rows = self.config_db_manager.execute_update_delete(query, tuple(values))
            return affected_rows is not None and affected_rows > 0

        except Exception as e:
            self.logger.error("Failed to delete %s by condition: %s", model_class.__name__, e)
            return False

    def find_by_id(self, model_class: Type[BaseModel], record_id: int,
                   select_columns: List[str] = None, ignore_columns: List[str] = None) -> Optional[BaseModel]:
        """ID로 레코드 조회"""
        try:
            table_name = model_class().get_table_name()
            db_type = self.config_db_manager.db_type

            if select_columns:
                columns_str = ", ".join(select_columns)
            elif ignore_columns:
                all_columns = ['id', 'created_at', 'updated_at'] + model_class().get_column_names()
                filtered_columns = [col for col in all_columns if col not in ignore_columns]
                columns_str = ", ".join(filtered_columns) if filtered_columns else "*"
            else:
                columns_str = "*"

            if db_type == "postgresql":
                query = f"SELECT {columns_str} FROM {table_name} WHERE id = %s"
            else:
                query = f"SELECT {columns_str} FROM {table_name} WHERE id = ?"

            result = self.config_db_manager.execute_query_one(query, (record_id,))

            if result:
                return model_class.from_dict(dict(result))
            return None

        except Exception as e:
            self.logger.error("Failed to find %s with id %s: %s",
                            model_class.__name__, record_id, e)
            return None

    def find_all(self, model_class: Type[BaseModel], limit: int = 500, offset: int = 0,
                 select_columns: List[str] = None, ignore_columns: List[str] = None,
                 join_user: bool = False) -> List[BaseModel]:
        """모든 레코드 조회 (페이징 지원)"""
        try:
            table_name = model_class().get_table_name()
            db_type = self.config_db_manager.db_type

            if join_user:
                if select_columns:
                    columns_str = ", ".join([f"t.{col}" for col in select_columns])
                else:
                    columns_str = "t.*"
                columns_str += ", u.username, u.full_name"
                from_clause = f"FROM {table_name} t LEFT JOIN users u ON t.user_id = u.id"
                orderby_field = "t.id"
            else:
                if select_columns:
                    columns_str = ", ".join(select_columns)
                elif ignore_columns:
                    all_columns = ['id', 'created_at', 'updated_at'] + model_class().get_column_names()
                    filtered_columns = [col for col in all_columns if col not in ignore_columns]
                    columns_str = ", ".join(filtered_columns) if filtered_columns else "*"
                else:
                    columns_str = "*"
                from_clause = f"FROM {table_name}"
                orderby_field = "id"

            if db_type == "postgresql":
                query = f"SELECT {columns_str} {from_clause} ORDER BY {orderby_field} DESC LIMIT %s OFFSET %s"
            else:
                query = f"SELECT {columns_str} {from_clause} ORDER BY {orderby_field} DESC LIMIT ? OFFSET ?"

            results = self.config_db_manager.execute_query(query, (limit, offset))

            return [model_class.from_dict(dict(row)) for row in results] if results else []

        except Exception as e:
            self.logger.error("Failed to find all %s: %s", model_class.__name__, e)
            return []

    def find_by_condition(self, model_class: Type[BaseModel],
                         conditions: Dict[str, Any],
                         limit: int = 500,
                         offset: int = 0,
                         orderby: str = "id",
                         orderby_asc: bool = False,
                         return_list: bool = False,
                         select_columns: List[str] = None,
                         ignore_columns: List[str] = None,
                         join_user: bool = False) -> List[BaseModel]:
        """조건으로 레코드 조회 - 자동 복구 포함"""
        def _do_find():
            table_name = model_class().get_table_name()
            db_type = self.config_db_manager.db_type

            where_clauses = []
            values = []

            for key, value in conditions.items():
                if join_user:
                    # 연산자 분리 후 t. 접두사 추가
                    base_key = key
                    suffix = ""
                    for op in ['__like__', '__notlike__', '__not__', '__gte__', '__lte__', '__gt__', '__lt__', '__in__', '__notin__', '__isnull__']:
                        if key.endswith(op):
                            base_key = key[:-len(op)]
                            suffix = op
                            break
                    prefixed_key = f"t.{base_key}{suffix}"
                    self._build_where_clause(prefixed_key, value, where_clauses, values, db_type)
                else:
                    self._build_where_clause(key, value, where_clauses, values, db_type)

            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"

            if db_type == "postgresql":
                limit_clause = "LIMIT %s OFFSET %s"
            else:
                limit_clause = "LIMIT ? OFFSET ?"

            values.extend([limit, offset])
            orderby_type = "ASC" if orderby_asc else "DESC"

            if join_user:
                if select_columns:
                    columns_str = ", ".join([f"t.{col}" for col in select_columns])
                else:
                    columns_str = "t.*"
                columns_str += ", u.username, u.full_name"
                from_clause = f"FROM {table_name} t LEFT JOIN users u ON t.user_id = u.id"
                orderby_field = f"t.{orderby}"
            else:
                if select_columns:
                    columns_str = ", ".join(select_columns)
                elif ignore_columns:
                    all_columns = ['id', 'created_at', 'updated_at'] + model_class().get_column_names()
                    filtered_columns = [col for col in all_columns if col not in ignore_columns]
                    columns_str = ", ".join(filtered_columns) if filtered_columns else "*"
                else:
                    columns_str = "*"
                from_clause = f"FROM {table_name}"
                orderby_field = orderby

            # 다중 컬럼 정렬 지원: 각 컬럼에 ASC/DESC 적용
            # 이미 ASC/DESC가 포함된 경우 중복 방지
            orderby_parts = [col.strip() for col in orderby_field.split(",")]
            cleaned_parts = []
            for col in orderby_parts:
                upper = col.upper()
                if upper.endswith(" DESC") or upper.endswith(" ASC"):
                    cleaned_parts.append(col)
                else:
                    cleaned_parts.append(f"{col} {orderby_type}")
            orderby_clause = ", ".join(cleaned_parts)

            query = f"SELECT {columns_str} {from_clause} WHERE {where_clause} ORDER BY {orderby_clause} {limit_clause}"

            results = self.config_db_manager.execute_query(query, tuple(values))

            if return_list:
                return [dict(row) for row in results] if results else []
            else:
                return [model_class.from_dict(dict(row)) for row in results] if results else []

        try:
            return self._with_auto_recovery(_do_find, "find_by_condition")
        except Exception as e:
            self.logger.error("Failed to find %s by condition: %s", model_class.__name__, e)
            return []

    def _build_where_clause(self, key: str, value: Any, where_clauses: List[str],
                           values: List[Any], db_type: str):
        """WHERE 절 조건 빌드 헬퍼"""
        placeholder = "%s" if db_type == "postgresql" else "?"

        if key.endswith("__like__"):
            actual_key = key[:-8]
            where_clauses.append(f"{actual_key} LIKE {placeholder}")
            values.append(f"%{value}%")
        elif key.endswith("__notlike__"):
            actual_key = key[:-11]
            where_clauses.append(f"{actual_key} NOT LIKE {placeholder}")
            values.append(f"%{value}%")
        elif key.endswith("__not__"):
            actual_key = key[:-7]
            where_clauses.append(f"{actual_key} != {placeholder}")
            values.append(value)
        elif key.endswith("__gte__"):
            actual_key = key[:-7]
            where_clauses.append(f"{actual_key} >= {placeholder}")
            values.append(value)
        elif key.endswith("__lte__"):
            actual_key = key[:-7]
            where_clauses.append(f"{actual_key} <= {placeholder}")
            values.append(value)
        elif key.endswith("__gt__"):
            actual_key = key[:-6]
            where_clauses.append(f"{actual_key} > {placeholder}")
            values.append(value)
        elif key.endswith("__lt__"):
            actual_key = key[:-6]
            where_clauses.append(f"{actual_key} < {placeholder}")
            values.append(value)
        elif key.endswith("__in__"):
            actual_key = key[:-6]
            if isinstance(value, (list, tuple)) and len(value) > 0:
                placeholders = ", ".join([placeholder] * len(value))
                where_clauses.append(f"{actual_key} IN ({placeholders})")
                values.extend(value)
        elif key.endswith("__notin__"):
            actual_key = key[:-9]
            if isinstance(value, (list, tuple)) and len(value) > 0:
                placeholders = ", ".join([placeholder] * len(value))
                where_clauses.append(f"{actual_key} NOT IN ({placeholders})")
                values.extend(value)
        else:
            where_clauses.append(f"{key} = {placeholder}")
            values.append(value)

    def update_list_columns(self, model_class: Type[BaseModel],
                           updates: Dict[str, Any], conditions: Dict[str, Any]) -> bool:
        """리스트 컬럼을 포함한 모델 업데이트"""
        try:
            table_name = model_class().get_table_name()
            db_type = self.config_db_manager.db_type
            placeholder = "%s" if db_type == "postgresql" else "?"

            set_clauses = []
            values = []

            for column, value in updates.items():
                if isinstance(value, list) and db_type == "postgresql":
                    set_clauses.append(f"{column} = {placeholder}::text[]")
                elif isinstance(value, dict) and db_type == "postgresql":
                    set_clauses.append(f"{column} = {placeholder}::jsonb")
                    value = json.dumps(value)
                else:
                    set_clauses.append(f"{column} = {placeholder}")
                values.append(value)

            where_clauses = []
            for key, value in conditions.items():
                where_clauses.append(f"{key} = {placeholder}")
                values.append(value)

            set_clause = ", ".join(set_clauses)
            where_clause = " AND ".join(where_clauses)

            query = f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}"

            affected_rows = self.config_db_manager.execute_update_delete(query, tuple(values))
            return affected_rows is not None and affected_rows > 0

        except Exception as e:
            self.logger.error("Failed to update list columns for %s: %s", model_class.__name__, e)
            return False

    def close(self):
        """데이터베이스 연결 종료"""
        self.config_db_manager.disconnect()
        self.logger.info("Application database connection closed")

    def run_migrations(self) -> bool:
        """데이터베이스 스키마 마이그레이션 실행"""
        try:
            return self.config_db_manager.run_migrations(self._models_registry)
        except Exception as e:
            self.logger.error("Failed to run migrations: %s", e)
            return False

    def get_table_list(self) -> List[Dict[str, Any]]:
        """데이터베이스의 모든 테이블 목록 조회"""
        try:
            db_type = self.config_db_manager.db_type

            if db_type == "postgresql":
                query = """
                    SELECT table_name, table_type
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """
            else:
                query = """
                    SELECT name as table_name, type as table_type
                    FROM sqlite_master
                    WHERE type='table'
                    ORDER BY name
                """

            results = self.config_db_manager.execute_query(query)
            return results if results else []

        except Exception as e:
            self.logger.error("Failed to get table list: %s", e)
            return []

    def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """테이블 스키마 조회"""
        try:
            db_type = self.config_db_manager.db_type

            if db_type == "postgresql":
                query = """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_name = %s
                    ORDER BY ordinal_position
                """
                results = self.config_db_manager.execute_query(query, (table_name,))
            else:
                query = f"PRAGMA table_info({table_name})"
                results = self.config_db_manager.execute_query(query)

            return results if results else []

        except Exception as e:
            self.logger.error("Failed to get table schema for %s: %s", table_name, e)
            return []

    def get_base_model_by_table_name(self, table_name: str) -> Optional[Type[BaseModel]]:
        """테이블 이름으로 BaseModel 클래스 생성 (DB 스키마 기반)"""
        try:
            schema = self.get_table_schema(table_name)
            if not schema:
                self.logger.warning(f"No schema found for table: {table_name}")
                return None

            db_type = self.config_db_manager.db_type

            type_mapping = {
                'integer': int,
                'bigint': int,
                'smallint': int,
                'numeric': float,
                'real': float,
                'double precision': float,
                'character varying': str,
                'varchar': str,
                'character': str,
                'char': str,
                'text': str,
                'boolean': bool,
                'timestamp without time zone': str,
                'timestamp with time zone': str,
                'date': str,
                'time': str,
                'json': dict,
                'jsonb': dict,
                'ARRAY': list,
                'INTEGER': int,
                'REAL': float,
                'TEXT': str,
                'BLOB': bytes,
            }

            fields = {}

            if db_type == "postgresql":
                for row in schema:
                    col_name = row['column_name']
                    data_type = row['data_type']
                    is_nullable = row['is_nullable'] == 'YES'
                    python_type = type_mapping.get(data_type, str)
                    if is_nullable:
                        fields[col_name] = (Optional[python_type], None)
                    else:
                        fields[col_name] = (python_type, ...)
            else:
                for row in schema:
                    col_name = row['name']
                    data_type = row['type']
                    notnull = row['notnull']
                    python_type = type_mapping.get(data_type.upper(), str)
                    if notnull:
                        fields[col_name] = (python_type, ...)
                    else:
                        fields[col_name] = (Optional[python_type], None)

            model_name = f"{table_name.capitalize()}Model"
            dynamic_model = create_model(model_name, **fields)

            return dynamic_model

        except Exception as e:
            self.logger.error("Failed to create BaseModel for table %s: %s", table_name, e)
            return None

    def execute_raw_query(self, query: str, params: tuple = None) -> Dict[str, Any]:
        """
        임의의 SQL 쿼리 실행 - 자동 복구 포함

        내부 네트워크 전용 API이므로 모든 쿼리 허용 (제한 없음)
        """
        def _do_execute():
            query_stripped = query.strip().rstrip(';')

            results = self.config_db_manager.execute_query(query_stripped, params)

            if results is not None:
                return {
                    "success": True,
                    "error": None,
                    "data": results,
                    "row_count": len(results)
                }
            else:
                return {
                    "success": False,
                    "error": "Query execution returned None",
                    "data": []
                }

        try:
            return self._with_auto_recovery(_do_execute, "execute_raw_query")
        except Exception as e:
            self.logger.error("Failed to execute raw query: %s", e)
            return {
                "success": False,
                "error": str(e),
                "data": []
            }

    # ========== 테이블 이름 기반 CRUD 메서드 (외부 컨테이너용) ==========

    def insert_record(self, table_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        테이블 이름 기반 레코드 삽입 - 자동 복구 포함

        Args:
            table_name: 테이블 이름
            data: 삽입할 데이터 딕셔너리

        Returns:
            Dict: {"success": bool, "id": int|None, "error": str|None}
        """
        def _do_insert():
            db_type = self.config_db_manager.db_type
            # dict/list 타입 직렬화
            serialized_data = self._serialize_data(data)
            columns = list(serialized_data.keys())
            values = list(serialized_data.values())

            if db_type == "postgresql":
                placeholders = ", ".join(["%s"] * len(columns))
                query = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id"
            else:
                placeholders = ", ".join(["?"] * len(columns))
                query = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"

            insert_id = self.config_db_manager.execute_insert(query, tuple(values))
            return {"success": True, "id": insert_id}

        try:
            return self._with_auto_recovery(_do_insert, "insert_record")
        except Exception as e:
            self.logger.error("Failed to insert record into %s: %s", table_name, e)
            return {"success": False, "id": None, "error": str(e)}

    def update_record(self, table_name: str, data: Dict[str, Any], record_id: int) -> Dict[str, Any]:
        """
        테이블 이름 기반 레코드 업데이트 (ID 기반) - 자동 복구 포함

        Args:
            table_name: 테이블 이름
            data: 업데이트할 데이터 딕셔너리
            record_id: 레코드 ID

        Returns:
            Dict: {"success": bool, "affected_rows": int, "error": str|None}
        """
        def _do_update():
            db_type = self.config_db_manager.db_type
            # dict/list 타입 직렬화
            serialized_data = self._serialize_data(data)

            if db_type == "postgresql":
                set_clause = ", ".join([f"{k} = %s" for k in serialized_data.keys()])
                query = f"UPDATE {table_name} SET {set_clause} WHERE id = %s"
            else:
                set_clause = ", ".join([f"{k} = ?" for k in serialized_data.keys()])
                query = f"UPDATE {table_name} SET {set_clause} WHERE id = ?"

            values = list(serialized_data.values()) + [record_id]
            affected_rows = self.config_db_manager.execute_update_delete(query, tuple(values))
            return {"success": True, "affected_rows": affected_rows or 0}

        try:
            return self._with_auto_recovery(_do_update, "update_record")
        except Exception as e:
            self.logger.error("Failed to update record in %s: %s", table_name, e)
            return {"success": False, "affected_rows": 0, "error": str(e)}

    def update_records_by_condition(self, table_name: str, updates: Dict[str, Any],
                                    conditions: Dict[str, Any]) -> Dict[str, Any]:
        """
        테이블 이름 기반 조건부 레코드 업데이트 - 자동 복구 포함

        지원하는 조건 연산자:
        - key: 동등 비교 (=)
        - key__like__: LIKE 검색
        - key__not__: 부정 (!=)
        - key__gte__: 크거나 같음 (>=)
        - key__lte__: 작거나 같음 (<=)
        - key__gt__: 큼 (>)
        - key__lt__: 작음 (<)
        - key__in__: IN 조건
        - key__notin__: NOT IN 조건

        Args:
            table_name: 테이블 이름
            updates: 업데이트할 필드와 값
            conditions: WHERE 조건

        Returns:
            Dict: {"success": bool, "affected_rows": int, "error": str|None}
        """
        def _do_update():
            db_type = self.config_db_manager.db_type
            # dict/list 타입 직렬화
            serialized_updates = self._serialize_data(updates)

            if db_type == "postgresql":
                set_clause = ", ".join([f"{k} = %s" for k in serialized_updates.keys()])
            else:
                set_clause = ", ".join([f"{k} = ?" for k in serialized_updates.keys()])

            # WHERE 절 빌드 (연산자 지원)
            where_clauses = []
            where_values = []
            for key, value in conditions.items():
                self._build_where_clause(key, value, where_clauses, where_values, db_type)

            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"

            query = f"UPDATE {table_name} SET {set_clause} WHERE {where_clause}"
            values = list(serialized_updates.values()) + where_values
            affected_rows = self.config_db_manager.execute_update_delete(query, tuple(values))
            return {"success": True, "affected_rows": affected_rows or 0}

        try:
            return self._with_auto_recovery(_do_update, "update_records_by_condition")
        except Exception as e:
            self.logger.error("Failed to update records in %s: %s", table_name, e)
            return {"success": False, "affected_rows": 0, "error": str(e)}

    def delete_record(self, table_name: str, record_id: int) -> Dict[str, Any]:
        """
        테이블 이름 기반 레코드 삭제 (ID 기반) - 자동 복구 포함

        Args:
            table_name: 테이블 이름
            record_id: 레코드 ID

        Returns:
            Dict: {"success": bool, "affected_rows": int, "error": str|None}
        """
        def _do_delete():
            db_type = self.config_db_manager.db_type

            if db_type == "postgresql":
                query = f"DELETE FROM {table_name} WHERE id = %s"
            else:
                query = f"DELETE FROM {table_name} WHERE id = ?"

            affected_rows = self.config_db_manager.execute_update_delete(query, (record_id,))
            return {"success": True, "affected_rows": affected_rows or 0}

        try:
            return self._with_auto_recovery(_do_delete, "delete_record")
        except Exception as e:
            self.logger.error("Failed to delete record from %s: %s", table_name, e)
            return {"success": False, "affected_rows": 0, "error": str(e)}

    def delete_records_by_condition(self, table_name: str, conditions: Dict[str, Any]) -> Dict[str, Any]:
        """
        테이블 이름 기반 조건부 레코드 삭제 - 자동 복구 포함

        지원하는 조건 연산자:
        - key: 동등 비교 (=)
        - key__like__: LIKE 검색
        - key__not__: 부정 (!=)
        - key__gte__: 크거나 같음 (>=)
        - key__lte__: 작거나 같음 (<=)
        - key__gt__: 큼 (>)
        - key__lt__: 작음 (<)
        - key__in__: IN 조건
        - key__notin__: NOT IN 조건

        Args:
            table_name: 테이블 이름
            conditions: WHERE 조건

        Returns:
            Dict: {"success": bool, "affected_rows": int, "error": str|None}
        """
        def _do_delete():
            db_type = self.config_db_manager.db_type

            # WHERE 절 빌드 (연산자 지원)
            where_clauses = []
            values = []
            for key, value in conditions.items():
                self._build_where_clause(key, value, where_clauses, values, db_type)

            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"

            query = f"DELETE FROM {table_name} WHERE {where_clause}"
            affected_rows = self.config_db_manager.execute_update_delete(query, tuple(values))
            return {"success": True, "affected_rows": affected_rows or 0}

        try:
            return self._with_auto_recovery(_do_delete, "delete_records_by_condition")
        except Exception as e:
            self.logger.error("Failed to delete records from %s: %s", table_name, e)
            return {"success": False, "affected_rows": 0, "error": str(e)}

    def find_record_by_id(self, table_name: str, record_id: int,
                          select_columns: List[str] = None) -> Dict[str, Any]:
        """
        테이블 이름 기반 ID로 레코드 조회 - 자동 복구 포함

        Args:
            table_name: 테이블 이름
            record_id: 레코드 ID
            select_columns: 조회할 컬럼 목록 (None이면 전체)

        Returns:
            Dict: {"success": bool, "data": dict|None, "error": str|None}
        """
        def _do_find():
            db_type = self.config_db_manager.db_type
            columns_str = ", ".join(select_columns) if select_columns else "*"

            if db_type == "postgresql":
                query = f"SELECT {columns_str} FROM {table_name} WHERE id = %s"
            else:
                query = f"SELECT {columns_str} FROM {table_name} WHERE id = ?"

            result = self.config_db_manager.execute_query_one(query, (record_id,))
            return {"success": True, "data": dict(result) if result else None}

        try:
            return self._with_auto_recovery(_do_find, "find_record_by_id")
        except Exception as e:
            self.logger.error("Failed to find record in %s: %s", table_name, e)
            return {"success": False, "data": None, "error": str(e)}

    def find_records(self, table_name: str, limit: int = 500, offset: int = 0,
                     select_columns: List[str] = None, ignore_columns: List[str] = None,
                     orderby: str = "id", orderby_asc: bool = False,
                     join_user: bool = False) -> Dict[str, Any]:
        """
        테이블 이름 기반 전체 레코드 조회 - 자동 복구 포함
        find_all 메서드와 동일한 로직 사용

        Args:
            table_name: 테이블 이름
            limit: 최대 조회 수
            offset: 시작 오프셋
            select_columns: 조회할 컬럼 목록 (None이면 전체)
            ignore_columns: 제외할 컬럼 목록
            orderby: 정렬 컬럼
            orderby_asc: 오름차순 정렬 여부
            join_user: users 테이블을 JOIN하여 username, full_name 조회 여부

        Returns:
            Dict: {"success": bool, "data": list, "row_count": int, "error": str|None}
        """
        def _do_find():
            db_type = self.config_db_manager.db_type
            orderby_type = "ASC" if orderby_asc else "DESC"

            # find_all과 동일한 컬럼 처리 로직
            if join_user:
                if select_columns:
                    columns_str = ", ".join([f"t.{col}" for col in select_columns])
                else:
                    columns_str = "t.*"
                columns_str += ", u.username, u.full_name"
                from_clause = f"{table_name} t LEFT JOIN users u ON t.user_id = u.id"
                orderby_field = f"t.{orderby}"
            else:
                if select_columns:
                    columns_str = ", ".join(select_columns)
                elif ignore_columns:
                    # ignore_columns가 있으면 테이블 스키마에서 해당 컬럼 제외
                    schema = self.get_table_schema(table_name)
                    if schema:
                        all_columns = [col['column_name'] for col in schema]
                        filtered_columns = [col for col in all_columns if col not in ignore_columns]
                        columns_str = ", ".join(filtered_columns) if filtered_columns else "*"
                    else:
                        columns_str = "*"
                else:
                    columns_str = "*"
                from_clause = table_name
                orderby_field = orderby

            # 이미 ASC/DESC가 포함된 경우 중복 방지
            upper_orderby = orderby_field.upper().strip()
            if upper_orderby.endswith(" DESC") or upper_orderby.endswith(" ASC"):
                effective_orderby = orderby_field
            else:
                effective_orderby = f"{orderby_field} {orderby_type}"

            if db_type == "postgresql":
                query = f"SELECT {columns_str} FROM {from_clause} ORDER BY {effective_orderby} LIMIT %s OFFSET %s"
            else:
                query = f"SELECT {columns_str} FROM {from_clause} ORDER BY {effective_orderby} LIMIT ? OFFSET ?"

            results = self.config_db_manager.execute_query(query, (limit, offset))
            data = [dict(row) for row in results] if results else []
            return {"success": True, "data": data, "row_count": len(data)}

        try:
            return self._with_auto_recovery(_do_find, "find_records")
        except Exception as e:
            self.logger.error("Failed to find records in %s: %s", table_name, e)
            return {"success": False, "data": [], "row_count": 0, "error": str(e)}

    def find_records_by_condition(self, table_name: str, conditions: Dict[str, Any],
                                   limit: int = 500, offset: int = 0,
                                   orderby: str = "id", orderby_asc: bool = False,
                                   select_columns: List[str] = None, ignore_columns: List[str] = None,
                                   join_user: bool = False) -> List[Dict[str, Any]]:
        """
        테이블 이름 기반 조건부 레코드 조회 - 자동 복구 포함
        find_by_condition 메서드와 동일한 로직 사용

        지원하는 조건 연산자:
        - key: 동등 비교 (=)
        - key__like__: LIKE 검색
        - key__not__: 부정 (!=)
        - key__gte__: 크거나 같음 (>=)
        - key__lte__: 작거나 같음 (<=)
        - key__gt__: 큼 (>)
        - key__lt__: 작음 (<)
        - key__in__: IN 조건
        - key__notin__: NOT IN 조건

        Args:
            table_name: 테이블 이름
            conditions: WHERE 조건 딕셔너리
            limit: 최대 조회 수
            offset: 시작 오프셋
            orderby: 정렬 컬럼
            orderby_asc: 오름차순 정렬 여부
            select_columns: 조회할 컬럼 목록 (None이면 전체)
            ignore_columns: 제외할 컬럼 목록
            join_user: users 테이블을 JOIN하여 username, full_name 조회 여부

        Returns:
            List[Dict]: 조회된 레코드 리스트 (빈 리스트 = 결과 없음, 에러 시 예외 발생)
        """
        def _do_find():
            db_type = self.config_db_manager.db_type
            orderby_type = "ASC" if orderby_asc else "DESC"

            # find_by_condition과 동일한 컬럼 처리 로직
            if join_user:
                if select_columns:
                    columns_str = ", ".join([f"t.{col}" for col in select_columns])
                else:
                    columns_str = "t.*"
                columns_str += ", u.username, u.full_name"
                from_clause = f"{table_name} t LEFT JOIN users u ON t.user_id = u.id"
                orderby_field = f"t.{orderby}"
            else:
                if select_columns:
                    columns_str = ", ".join(select_columns)
                elif ignore_columns:
                    # ignore_columns가 있으면 테이블 스키마에서 해당 컬럼 제외
                    schema = self.get_table_schema(table_name)
                    if schema:
                        all_columns = [col['column_name'] for col in schema]
                        filtered_columns = [col for col in all_columns if col not in ignore_columns]
                        columns_str = ", ".join(filtered_columns) if filtered_columns else "*"
                    else:
                        columns_str = "*"
                else:
                    columns_str = "*"
                from_clause = table_name
                orderby_field = orderby

            # WHERE 절 빌드
            where_clauses = []
            values = []

            for key, value in conditions.items():
                if join_user:
                    # 연산자 분리 후 t. 접두사 추가
                    base_key = key
                    suffix = ""
                    for op in ['__like__', '__notlike__', '__not__', '__gte__', '__lte__', '__gt__', '__lt__', '__in__', '__notin__', '__isnull__']:
                        if key.endswith(op):
                            base_key = key[:-len(op)]
                            suffix = op
                            break
                    prefixed_key = f"t.{base_key}{suffix}"
                    self._build_where_clause(prefixed_key, value, where_clauses, values, db_type)
                else:
                    self._build_where_clause(key, value, where_clauses, values, db_type)

            where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
            values.extend([limit, offset])

            # 이미 ASC/DESC가 포함된 경우 중복 방지
            upper_orderby = orderby_field.upper().strip()
            if upper_orderby.endswith(" DESC") or upper_orderby.endswith(" ASC"):
                effective_orderby = orderby_field
            else:
                effective_orderby = f"{orderby_field} {orderby_type}"

            if db_type == "postgresql":
                query = f"SELECT {columns_str} FROM {from_clause} WHERE {where_clause} ORDER BY {effective_orderby} LIMIT %s OFFSET %s"
            else:
                query = f"SELECT {columns_str} FROM {from_clause} WHERE {where_clause} ORDER BY {effective_orderby} LIMIT ? OFFSET ?"

            results = self.config_db_manager.execute_query(query, tuple(values))
            data = [dict(row) for row in results] if results else []
            return data

        try:
            return self._with_auto_recovery(_do_find, "find_records_by_condition")
        except Exception as e:
            self.logger.error("Failed to find records in %s: %s", table_name, e)
            raise

    # ========== Legacy async query helpers (named params 지원) ==========
    def _convert_named_params(self, query: str, params: Optional[Dict[str, Any]] = None) -> tuple[str, tuple]:
        """
        :name 형태 named parameter를 DB 드라이버 파라미터 스타일로 변환.

        PostgreSQL: %s
        SQLite: ?
        """
        if not params:
            return query, tuple()

        placeholder = "%s" if self.config_db_manager.db_type == "postgresql" else "?"
        keys: List[str] = []

        def _repl(match):
            key = match.group(1)
            if key not in params:
                raise KeyError(f"Missing SQL parameter: {key}")
            keys.append(key)
            return placeholder

        converted_query = re.sub(r":([a-zA-Z_][a-zA-Z0-9_]*)", _repl, query)
        converted_params = tuple(params[key] for key in keys)
        return converted_query, converted_params

    async def fetch_all(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        converted_query, converted_params = self._convert_named_params(query, params)
        result = self.config_db_manager.execute_query(converted_query, converted_params)
        return [dict(row) for row in result] if result else []

    async def fetch_one(self, query: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        converted_query, converted_params = self._convert_named_params(query, params)
        result = self.config_db_manager.execute_query_one(converted_query, converted_params)
        return dict(result) if result else None

    async def execute(self, query: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        converted_query, converted_params = self._convert_named_params(query, params)
        upper_query = converted_query.strip().upper()

        if upper_query.startswith("INSERT"):
            insert_query = converted_query
            if self.config_db_manager.db_type == "postgresql" and "RETURNING" not in upper_query:
                insert_query = f"{converted_query.rstrip(';')} RETURNING id"
            insert_id = self.config_db_manager.execute_insert(insert_query, converted_params)
            return {"success": True, "id": insert_id}

        if upper_query.startswith("UPDATE") or upper_query.startswith("DELETE"):
            affected = self.config_db_manager.execute_update_delete(converted_query, converted_params)
            return {"success": True, "affected_rows": affected or 0}

        # DDL/기타 쿼리
        self.config_db_manager.execute_query(converted_query, converted_params)
        return {"success": True}


# 하위 호환 alias
AppDatabaseManager = XgenDB
