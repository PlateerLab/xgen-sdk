"""
xgen_sdk.config.base_config — Config 기반 클래스 + PersistentConfig

모든 설정 카테고리(sub_config)의 기반 클래스.
PersistentConfig는 DB → Redis → Default 순서로 값을 로드하고
dual-write로 양쪽을 동기화합니다.

Usage:
    from xgen_sdk.config import BaseConfig, PersistentConfig

    class OpenAIConfig(BaseConfig):
        def initialize(self):
            self.API_KEY = self.create_persistent_config(
                env_name="OPENAI_API_KEY",
                config_path="openai.api_key",
                default_value=""
            )
"""
import os
import json
import logging
from typing import Any, Optional, Union, List, Dict
from abc import ABC, abstractmethod
from xgen_sdk.config.redis_config import RedisConfigManager
from xgen_sdk.db.config_serializer import normalize_config_value

logger = logging.getLogger("xgen-sdk.config-base")


# ============================================================== #
# DB 연결 확인 유틸리티
# ============================================================== #

def _is_db_available(db_manager) -> bool:
    """DB 연결이 사용 가능한지 확인 (psycopg3 호환)"""
    if db_manager is None:
        return False
    actual_manager = db_manager
    if hasattr(db_manager, 'config_db_manager'):
        actual_manager = db_manager.config_db_manager
    if hasattr(actual_manager, '_is_pool_healthy'):
        return actual_manager._is_pool_healthy()
    elif hasattr(actual_manager, 'db_type') and actual_manager.db_type == 'sqlite':
        return getattr(actual_manager, '_sqlite_connection', None) is not None
    elif hasattr(actual_manager, 'connection') and actual_manager.connection:
        return True
    return False


# ============================================================== #
# PersistentConfig — DB/Redis dual-sync 설정 컨테이너
# ============================================================== #

class PersistentConfig:
    """설정 값을 담는 데이터 컨테이너 — DB → Redis → Default 로드, dual-write 동기화"""

    def __init__(self, env_name: str, config_path: str, env_value: Any,
                 type_converter: Optional[callable] = None,
                 redis_manager: Optional[RedisConfigManager] = None,
                 db_manager=None):
        self.env_name = env_name
        self.config_path = config_path
        self.env_value = env_value
        self.type_converter = type_converter
        self.redis_manager = redis_manager or RedisConfigManager()
        self.db_manager = db_manager
        self._value = self._load_value()

    def _load_value(self) -> Any:
        """DB → Redis → 기본값 순서로 설정 값 로드"""
        try:
            category = self.config_path.split('.')[0] if '.' in self.config_path else 'unknown'
            expected_type = self._infer_data_type(self.env_value)

            # 1. DB에서 먼저 확인
            if _is_db_available(self.db_manager):
                from xgen_sdk.db.db_config_helper import get_db_config
                db_value = get_db_config(self.db_manager, self.config_path)
                if db_value is not None:
                    db_value = normalize_config_value(db_value, expected_type)
                    logger.info(f"[DB] [{category}] {self.env_name} | path={self.config_path} | value={db_value}")
                    self.redis_manager.set_config(
                        config_path=self.config_path,
                        config_value=db_value,
                        data_type=self._infer_data_type(db_value),
                        env_name=self.env_name
                    )
                    if self.type_converter:
                        return self.type_converter(db_value)
                    return db_value

            # 2. Redis에서 확인
            redis_value = self.redis_manager.get_config_value(self.env_name)
            if redis_value is not None:
                redis_value = normalize_config_value(redis_value, expected_type)
                logger.info(f"[Redis] [{category}] {self.env_name} | path={self.config_path} | value={redis_value}")
                if _is_db_available(self.db_manager):
                    from xgen_sdk.db.db_config_helper import set_db_config
                    set_db_config(self.db_manager, self.config_path, redis_value,
                                  self._infer_data_type(redis_value), self.env_name)
                if self.type_converter:
                    return self.type_converter(redis_value)
                return redis_value

            # 3. 기본값 사용 및 Redis/DB에 저장
            logger.info(f"[Default] [{category}] {self.env_name} | path={self.config_path} | value={self.env_value}")
            self.redis_manager.set_config(
                config_path=self.config_path,
                config_value=self.env_value,
                data_type=self._infer_data_type(self.env_value),
                env_name=self.env_name
            )
            if _is_db_available(self.db_manager):
                from xgen_sdk.db.db_config_helper import set_db_config
                set_db_config(self.db_manager, self.config_path, self.env_value,
                              self._infer_data_type(self.env_value), self.env_name)
            return self.env_value

        except Exception as e:
            logger.warning(f"Failed to load value for {self.config_path}: {e}")
            return self.env_value

    def _load_from_redis(self) -> Any:
        """하위호환용"""
        return self._load_value()

    def _infer_data_type(self, value: Any) -> str:
        if isinstance(value, bool):
            return "bool"
        elif isinstance(value, int):
            return "int"
        elif isinstance(value, float):
            return "float"
        elif isinstance(value, list):
            return "list"
        elif isinstance(value, dict):
            return "dict"
        return "string"

    @property
    def value(self) -> Any:
        return self._value

    @value.setter
    def value(self, new_value: Any):
        if self.type_converter:
            new_value = self.type_converter(new_value)
        self._value = new_value

    def refresh(self):
        """DB/Redis에서 최신 값 다시 로드"""
        self._value = self._load_value()


# ============================================================== #
# BaseConfig — 설정 카테고리 기반 클래스
# ============================================================== #

class BaseConfig(ABC):
    """모든 설정 카테고리 클래스의 기반 (sub_config에서 상속)"""

    def __init__(self, redis_manager: Optional[RedisConfigManager] = None, db_manager=None):
        self.configs: Dict[str, PersistentConfig] = {}
        self.redis_manager = redis_manager or RedisConfigManager()
        self.db_manager = db_manager
        self.logger = logging.getLogger(f"config-{self.__class__.__name__.lower()}")
        try:
            self.initialize()
        except Exception as e:
            self.logger.error(f"Failed to initialize config: {e}")
            raise

    @abstractmethod
    def initialize(self) -> Dict[str, PersistentConfig]:
        pass

    def get_env_value(self, env_name: str, default_value: Any,
                      file_path: Optional[str] = None,
                      type_converter: Optional[callable] = None) -> Any:
        """환경변수 → 파일 → 기본값 순서로 값 로드"""
        env_value = os.environ.get(env_name)
        if env_value is not None:
            try:
                if type_converter:
                    return type_converter(env_value)
                return env_value
            except (ValueError, TypeError):
                pass

        if file_path and os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    file_value = f.read().strip()
                    if file_value:
                        if type_converter:
                            return type_converter(file_value)
                        os.environ[env_name] = file_value
                        return file_value
            except (IOError, OSError):
                pass

        return default_value

    def create_persistent_config(self, env_name: str, config_path: str,
                                 default_value: Any, file_path: Optional[str] = None,
                                 type_converter: Optional[callable] = None) -> PersistentConfig:
        """PersistentConfig 객체 생성 (Redis + DB 기반)"""
        env_value = self.get_env_value(env_name, default_value, file_path, type_converter)
        config = PersistentConfig(
            env_name=env_name,
            config_path=config_path,
            env_value=env_value,
            type_converter=type_converter,
            redis_manager=self.redis_manager,
            db_manager=self.db_manager
        )
        self.configs[env_name] = config
        return config

    def __getitem__(self, key: str) -> PersistentConfig:
        if key in self.configs:
            return self.configs[key]
        raise KeyError(f"Configuration '{key}' not found in {self.__class__.__name__}")

    def get_config_summary(self) -> Dict[str, Any]:
        return {
            "class_name": self.__class__.__name__,
            "config_count": len(self.configs),
            "configs": {
                name: {
                    "current_value": config.value,
                    "default_value": config.env_value,
                    "config_path": config.config_path
                }
                for name, config in self.configs.items()
            }
        }


# ============================================================== #
# 타입 변환 함수
# ============================================================== #

def convert_to_str(value: Any) -> str:
    return str(value)

def convert_to_int(value: Union[str, int]) -> int:
    return int(value)

def convert_to_float(value: Union[str, float]) -> float:
    return float(value)

def convert_to_bool(value: Union[str, bool]) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ('true', '1', 'yes', 'on', 'enabled')

def convert_to_list(value: Union[str, list], separator: str = ',') -> List[str]:
    if isinstance(value, list):
        return value
    return [item.strip() for item in str(value).split(separator) if item.strip()]

def convert_to_int_list(value: Union[str, List[int]], separator: str = ',') -> List[int]:
    if isinstance(value, list):
        result = []
        for item in value:
            try:
                result.append(int(item))
            except (ValueError, TypeError):
                pass
        return result
    elif isinstance(value, str):
        value = value.strip()
        if value.startswith('[') and value.endswith(']'):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [int(item) for item in parsed]
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        result = []
        for p in value.split(separator):
            p = p.strip()
            try:
                result.append(int(p))
            except ValueError:
                pass
        return result
    return []
