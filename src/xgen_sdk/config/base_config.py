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
import time
from typing import Any, Optional, Union, List, Dict
from abc import ABC, abstractmethod
from xgen_sdk.config.redis_config import RedisConfigManager
from xgen_sdk.db.config_serializer import normalize_config_value

logger = logging.getLogger("xgen-sdk.config-base")


# ============================================================== #
# Multi-Pod 캐시 인밸리데이션 — Version Sentinel
# ============================================================== #
#
# 문제: 멀티 Pod 환경에서 한 Pod 가 config 를 PUT 으로 갱신하면 그 Pod 의
# in-memory PersistentConfig._value 만 바뀌고, 다른 Pod 들은 boot 시 적재한
# stale 값을 계속 반환한다. (Redis/DB 는 정합이지만 캐시가 stale.)
#
# 해법: RedisConfigManager 가 모든 write 시점에 글로벌 카운터
# `config:_meta:version` 을 INCR 한다. 각 PersistentConfig 는 자기가 본 마지막
# 버전(_last_seen_version) 을 들고 있다가 `.value` 접근 시점에 현재 버전과
# 비교 — 다르면 _load_value() 로 lazy refresh.
#
# 핫패스 비용을 억제하기 위해 PersistentConfig 클래스 레벨에서 version 읽기를
# 50ms TTL 로 메모이즈 한다. 동일 request 안의 다중 `.value` 접근이 Redis 를
# 1회만 두드리도록.
# ============================================================== #

_VERSION_CACHE_TTL_S: float = 0.05  # 50ms — 사용자 체감 지연 < 1 frame
_version_cache_value: Optional[int] = None
_version_cache_ts: float = 0.0


def _invalidate_version_cache() -> None:
    """클래스 레벨 version 캐시 무효화 — write 직후 호출하여 강제 재조회 유도."""
    global _version_cache_ts
    _version_cache_ts = 0.0


def _read_version_cached(redis_manager) -> int:
    """글로벌 config version 을 50ms TTL 로 메모이즈하여 반환.

    `redis_manager` 가 `get_config_version` 을 지원하지 않거나 호출 실패 시
    이전 캐시 값이 있으면 그대로 반환, 없으면 0. 절대 예외를 propagate 하지
    않는다 — version sentinel 은 best-effort 메커니즘이며 장애 시에는
    graceful 하게 in-memory 캐시를 유지하는 쪽이 안전하다.
    """
    global _version_cache_value, _version_cache_ts
    now = time.monotonic()
    if _version_cache_value is not None and (now - _version_cache_ts) < _VERSION_CACHE_TTL_S:
        return _version_cache_value
    try:
        if redis_manager is not None and hasattr(redis_manager, 'get_config_version'):
            v = redis_manager.get_config_version()
        else:
            v = 0
    except Exception as e:
        logger.debug("get_config_version failed (graceful fallback): %s", e)
        return _version_cache_value if _version_cache_value is not None else 0
    _version_cache_value = v
    _version_cache_ts = now
    return v


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
        # Multi-Pod 캐시 인밸리데이션 — 자신이 마지막으로 본 글로벌 config version.
        # `.value` 접근 시 현재 version 과 비교해 변화가 있으면 lazy refresh.
        # _load_value() 가 끝난 직후에 읽어 최신값을 박아 둔다.
        try:
            self._last_seen_version: int = _read_version_cached(self.redis_manager)
        except Exception:
            self._last_seen_version = 0

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
                    # Redis 에 값이 없을 때만 restore. 이미 있으면 set 을 skip 해
                    # 불필요한 version sentinel INCR (멀티-Pod stampede 원인) 방지.
                    try:
                        redis_has_key = self.redis_manager.exists(self.env_name)
                    except Exception:
                        redis_has_key = False
                    if not redis_has_key:
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
        """현재 설정 값 반환.

        Multi-Pod 안전성 — 호출 시점에 글로벌 config version sentinel 을 확인하고
        직전에 본 버전과 다르면 _load_value() 로 lazy refresh 한다. version 조회는
        50ms TTL 메모이즈가 되어 있어 핫패스의 다중 `.value` 접근도 Redis GET 1회로
        압축된다. version 조회/refresh 실패는 silent — 마지막으로 적재된 in-memory
        캐시 값을 그대로 반환하여 Redis 일시 장애 시에도 서비스가 멈추지 않게 한다.
        """
        try:
            current = _read_version_cached(self.redis_manager)
            if current != self._last_seen_version:
                refreshed = self._load_value()
                self._value = refreshed
                # _load_value 가 default-restore 등으로 version 을 추가로 bump 했을
                # 가능성이 있으므로 캐시 무효화 후 최신값을 다시 읽어 박는다.
                # (그렇지 않으면 다음 .value 접근에서 또 다시 mismatch 로 인식.)
                _invalidate_version_cache()
                try:
                    self._last_seen_version = _read_version_cached(self.redis_manager)
                except Exception:
                    self._last_seen_version = current
        except Exception as e:
            # version sentinel 은 best-effort. 실패해도 기존 _value 그대로 반환.
            logger.debug("Version sentinel check failed for %s: %s", self.env_name, e)
        return self._value

    @value.setter
    def value(self, new_value: Any):
        """In-memory 캐시 값을 설정. Redis/DB 동기화는 호출처(ConfigComposer.update_config
        또는 RedisConfigManager.set_config) 에서 명시적으로 수행해야 한다.

        본 setter 자체는 version sentinel 을 건드리지 않는다 — version bump 는
        RedisConfigManager.set_config 안에서 atomic 하게 일어나며, 호출처가
        설정 직후 `_last_seen_version` 을 갱신할 책임을 진다.
        """
        if self.type_converter:
            new_value = self.type_converter(new_value)
        self._value = new_value

    def refresh(self):
        """DB/Redis 에서 최신 값 다시 로드 + version sentinel 동기화.

        명시적으로 호출되면 version 캐시 TTL 을 무시하고 강제로 _load_value 를 돈다.
        호출 후 `_last_seen_version` 은 현재 Redis 의 글로벌 version 으로 맞춰진다.
        """
        self._value = self._load_value()
        try:
            self._last_seen_version = _read_version_cached(self.redis_manager)
        except Exception as e:
            logger.debug("Failed to update _last_seen_version on refresh for %s: %s",
                         self.env_name, e)


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

def convert_to_dict(value: Union[str, dict]) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        value = value.strip()
        if value:
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
    return {}
