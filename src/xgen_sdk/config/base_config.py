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

#: 값을 로그에 그대로 남기면 안 되는 config 이름 조각.
#: config 로더는 부팅/refresh 마다 값을 INFO 로 남기는데, 여기에 API 키·
#: 비밀번호가 평문으로 찍히면 로그 수집기(Loki 등)에 그대로 적재된다.
_SENSITIVE_NAME_PARTS = (
    "KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL",
    "PRIVATE", "SALT", "CERT", "DSN",
)


def _mask_config_value(env_name: str, value: Any) -> Any:
    """민감한 이름의 config 값을 로그용으로 마스킹.

    값의 존재/길이는 남긴다 — 운영에서 "값이 비어 있어서 생긴 문제" 를
    진단하려면 그 정도는 필요하고, 그 이상은 유출이다.
    """
    name = (env_name or "").upper()
    if not any(part in name for part in _SENSITIVE_NAME_PARTS):
        return value
    if value is None:
        return "<unset>"
    text = str(value)
    if not text:
        return "<empty>"
    return f"<redacted len={len(text)}>"


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
    """설정 값을 담는 데이터 컨테이너 — DB → Redis → Default 로드, dual-write 동기화

    options:
        문자열 enum / dict 설정의 허용 값 리스트. 지정 시 프론트 환경설정 UI 가 자유 텍스트
        입력 대신 셀렉터(드롭다운) 로 자동 렌더한다. 백엔드는 validation 을 강제하지 않으며
        (caller 측 책임), 단순히 UI 힌트로만 사용한다.
        형식 — string 리스트 또는 `[{"value": "x", "label": "X"}, ...]` 객체 리스트 모두 지원.
    options_loader:
        callable 지정 시 정적 options 대신 lazy 평가. property `.options` 접근 시 호출되어
        외부 카탈로그(예: LLM 모델 list API) 결과로 옵션을 동적으로 채운다.
        결과는 in-process 캐시 (TTL=options_cache_ttl). 실패 시 정적 `options` 로 fallback.
    options_cache_ttl:
        options_loader 결과 캐시 TTL (초). 기본 3600 (1시간).
    description / label:
        프론트 환경설정 UI 에서 카드 부제목 / 도움말로 노출되는 선택적 메타. 둘 다 영어/한글
        free-form 문자열. 없으면 UI 가 env_name 만 노출.
    """

    def __init__(self, env_name: str, config_path: str, env_value: Any,
                 type_converter: Optional[callable] = None,
                 redis_manager: Optional[RedisConfigManager] = None,
                 db_manager=None,
                 options: Optional[list] = None,
                 description: Optional[str] = None,
                 label: Optional[str] = None,
                 options_loader: Optional[callable] = None,
                 options_cache_ttl: int = 3600):
        self.env_name = env_name
        self.config_path = config_path
        self.env_value = env_value
        self.type_converter = type_converter
        self.redis_manager = redis_manager or RedisConfigManager()
        self.db_manager = db_manager
        # UI 메타데이터 — 백엔드 동작에는 영향 없음, 환경설정 편집 UI 렌더 힌트 전용.
        # _static_options 는 options_loader 가 실패할 때의 최종 fallback.
        self._static_options = list(options) if options else None
        self._options_loader = options_loader
        self._options_cache_ttl = options_cache_ttl if options_cache_ttl and options_cache_ttl > 0 else 3600
        self._options_cache_value: Optional[list] = None
        self._options_cache_ts: float = 0.0
        self.description = description
        self.label = label
        #: 마지막 적재가 저장소에서 실제로 읽혔는가. False 면 지금 들고 있는 것은
        #: 등록 기본값이고, 다음 접근에서 다시 시도한다.
        self._loaded_ok: bool = True
        self._value = self._load_value()
        # Multi-Pod 캐시 인밸리데이션 — 자신이 마지막으로 본 글로벌 config version.
        # `.value` 접근 시 현재 version 과 비교해 변화가 있으면 lazy refresh.
        # _load_value() 가 끝난 직후에 읽어 최신값을 박아 둔다.
        try:
            self._last_seen_version: int = _read_version_cached(self.redis_manager)
        except Exception:
            self._last_seen_version = 0

    def _load_value(self) -> Any:
        """최초 적재용 — 값 하나를 돌려준다 (못 읽으면 등록 기본값).

        갱신에는 :meth:`_reload` 를 쓴다. 갱신 때 못 읽었다고 기본값을 박으면
        관리자가 설정해 둔 값이 **메모리에서** 기본값으로 뒤집힌다.
        """
        status, value = self._resolve()
        self._loaded_ok = status != "error"
        return self.env_value if status == "error" else value

    def _reload(self) -> bool:
        """저장소에서 다시 읽어 캐시를 갱신한다. **못 읽으면 기존 값을 지킨다.**

        Redis 재연결 직후처럼 저장소가 아직 흔들릴 때 refresh 가 돌면, 여기서
        기본값을 박는 순간 그 파드의 모든 설정이 조용히 기본값으로 뒤집힌다.
        읽지 못한 것은 "값이 바뀌었다" 가 아니다.
        """
        status, value = self._resolve()
        if status == "error":
            self._loaded_ok = False
            logger.warning(
                "[Unavailable] %s | path=%s | 저장소를 읽지 못해 직전 값을 유지한다",
                self.env_name, self.config_path,
            )
            return False
        self._value = value
        self._loaded_ok = True
        return True

    def _resolve(self):
        """(상태, 값) — **Redis → DB → 기본값**. 상태는 ``"ok"`` | ``"error"``.

        읽는 순서는 시스템 전체에서 하나여야 한다 (1.41.0). 예전에는 여기만 DB 를
        먼저 봤고 위성 파드(ConfigClient)는 Redis 만 봤다. 같은 키를 두 계층이
        서로 다른 저장소에서 읽으니, Redis 에서 키가 빠진 순간 관리자 화면은
        [설정됨] 인데 워크플로우 파드만 빈 값을 보는 상태가 생겼다.

        이제 두 계층이 같은 사다리(:meth:`RedisConfigManager.probe_value`)를 탄다.
        Redis 가 정본 캐시, DB 가 영속 저장소이고, DB 에서 찾으면 Redis 로 되살린다.

        ⚠ **못 읽었을 때 기본값을 쓰지 않는다.** 저장소 장애를 "설정 안 함" 으로
        오독해 기본값을 Redis/DB 에 써 버리면, 관리자가 넣어 둔 값이 그 순간
        기본값으로 덮인다. 그럴 때는 메모리에만 기본값을 두고 **아무 데도 쓰지
        않는다** — 저장소가 돌아오면 다음 refresh 가 진짜 값을 가져온다.
        """
        try:
            category = self.config_path.split('.')[0] if '.' in self.config_path else 'unknown'
            expected_type = self._infer_data_type(self.env_value)

            status, value, source = self._probe()
            if status == "ok" and value is not None:
                value = normalize_config_value(value, expected_type)
                logger.info(
                    f"[{source or 'store'}] [{category}] {self.env_name} | path={self.config_path} "
                    f"| value={_mask_config_value(self.env_name, value)}"
                )
                # Redis 에서 읽었는데 DB 에 없을 수 있다(과거 Redis-only 쓰기 잔재).
                # 영속 저장소를 채워 둔다 — 다음 Redis 유실 때 이 값이 살아남는다.
                if source == "redis" and _is_db_available(self.db_manager):
                    try:
                        from xgen_sdk.db.db_config_helper import set_db_config
                        set_db_config(self.db_manager, self.config_path, value,
                                      self._infer_data_type(value), self.env_name)
                    except Exception as db_err:  # noqa: BLE001
                        logger.debug(f"DB write-through 실패({self.env_name}): {db_err}")
                if self.type_converter:
                    return ("ok", self.type_converter(value))
                return ("ok", value)

            if status == "error":
                # 저장소가 답하지 못했다 — 기본값을 **쓰지 않는다**. 호출자가
                # 직전 값을 지킬지(갱신) 기본값으로 시작할지(최초 적재) 정한다.
                return ("error", None)

            # 3. 기본값 사용 및 Redis/DB에 저장 (진짜로 아무 데도 없을 때만)
            logger.info(f"[Default] [{category}] {self.env_name} | path={self.config_path} | value={_mask_config_value(self.env_name, self.env_value)}")
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
            return ("ok", self.env_value)

        except Exception as e:
            logger.warning(f"Failed to load value for {self.config_path}: {e}")
            return ("error", None)

    def _probe(self):
        """(상태, 값, 출처) — 매니저의 공통 사다리를 쓴다. 구버전 매니저도 받아 준다."""
        probe = getattr(self.redis_manager, "probe_value", None)
        if callable(probe):
            try:
                return probe(self.env_name, self.config_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"probe_value 실패({self.env_name}): {e}")
                return ("error", None, "")
        # 사다리를 모르는 매니저 — 예전 동작(값만 조회)으로 내려간다.
        try:
            value = self.redis_manager.get_config_value(self.env_name)
        except Exception:  # noqa: BLE001
            return ("error", None, "")
        return ("ok", value, "redis") if value is not None else ("missing", None, "")

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
            # 최초 적재가 실패했으면(저장소 장애 중 기동) version 변화를 기다리지 않고
            # 성공할 때까지 매 접근마다 다시 시도한다 — 그러지 않으면 이 파드는
            # version 이 바뀌기 전까지 **기본값**으로 계속 돈다.
            if not getattr(self, "_loaded_ok", True):
                if self._reload():
                    _invalidate_version_cache()
                    try:
                        self._last_seen_version = _read_version_cached(self.redis_manager)
                    except Exception:
                        pass
                return self._value

            current = _read_version_cached(self.redis_manager)
            if current != self._last_seen_version:
                # 못 읽으면 직전 값을 지킨다(_reload). 그때는 sentinel 도 옮기지
                # 않는다 — 옮기면 "갱신했다" 는 거짓말이 되어 다음 기회를 잃는다.
                if self._reload():
                    # _reload 가 default-restore 등으로 version 을 추가로 bump 했을
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
        # 못 읽으면 직전 값을 지킨다 — 갱신 실패는 "값이 바뀌었다" 가 아니다.
        if not self._reload():
            return
        try:
            self._last_seen_version = _read_version_cached(self.redis_manager)
        except Exception as e:
            logger.debug("Failed to update _last_seen_version on refresh for %s: %s",
                         self.env_name, e)

    # ─────────────────────────────────────────────────────────────
    # options — lazy 평가 + in-process 캐시 (TTL=options_cache_ttl).
    # options_loader 가 지정되어 있으면 동적 호출, 없으면 정적 _static_options.
    # 어떤 단계에서도 raise 하지 않음 — 실패 시 stale 캐시 / 정적 fallback.
    # ─────────────────────────────────────────────────────────────
    @property
    def options(self) -> Optional[list]:
        """현재 옵션 리스트. options_loader 가 있으면 lazy 평가 + 캐시."""
        if self._options_loader is None:
            return list(self._static_options) if self._static_options else None

        now = time.time()
        if (
            self._options_cache_value is not None
            and (now - self._options_cache_ts) < self._options_cache_ttl
        ):
            return list(self._options_cache_value)

        try:
            loaded = self._options_loader()
            if loaded:
                self._options_cache_value = list(loaded)
                self._options_cache_ts = now
                return list(self._options_cache_value)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("options_loader failed (path=%s): %s", self.config_path, e)

        # loader 실패 또는 빈 결과 — 기존 캐시 우선, 없으면 정적 fallback
        if self._options_cache_value:
            return list(self._options_cache_value)
        if self._static_options:
            return list(self._static_options)
        return None

    @options.setter
    def options(self, new_value: Optional[list]) -> None:
        """정적 options 변경 — 주로 테스트/runtime override 용도."""
        self._static_options = list(new_value) if new_value else None
        # loader 캐시는 그대로 둠. loader 가 우선.


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
                                 type_converter: Optional[callable] = None,
                                 options: Optional[list] = None,
                                 description: Optional[str] = None,
                                 label: Optional[str] = None,
                                 options_loader: Optional[callable] = None,
                                 options_cache_ttl: int = 3600) -> PersistentConfig:
        """PersistentConfig 객체 생성 (Redis + DB 기반).

        options: 정적 옵션 리스트 (문자열 또는 dict). 프론트가 셀렉터로 렌더.
        options_loader: 동적 옵션 로더 callable. 지정 시 lazy 평가 + 캐시 (TTL=options_cache_ttl).
                        실패 시 정적 `options` 로 fallback.
        options_cache_ttl: options_loader 결과 캐시 TTL (초). 기본 3600.
        description / label: 환경설정 UI 부제목/도움말 (선택).
        """
        env_value = self.get_env_value(env_name, default_value, file_path, type_converter)
        config = PersistentConfig(
            env_name=env_name,
            config_path=config_path,
            env_value=env_value,
            type_converter=type_converter,
            redis_manager=self.redis_manager,
            db_manager=self.db_manager,
            options=options,
            description=description,
            label=label,
            options_loader=options_loader,
            options_cache_ttl=options_cache_ttl,
        )
        self.configs[env_name] = config
        return config

    def __getitem__(self, key: str) -> PersistentConfig:
        if key in self.configs:
            return self.configs[key]
        raise KeyError(f"Configuration '{key}' not found in {self.__class__.__name__}")

    def get_config_summary(self) -> Dict[str, Any]:
        def _entry(config: PersistentConfig) -> Dict[str, Any]:
            d = {
                "current_value": config.value,
                "default_value": config.env_value,
                "config_path": config.config_path,
            }
            # UI 메타 — 셀렉터/도움말 렌더용 (선택).
            if getattr(config, 'options', None):
                d["options"] = list(config.options)
            if getattr(config, 'description', None):
                d["description"] = config.description
            if getattr(config, 'label', None):
                d["label"] = config.label
            return d

        return {
            "class_name": self.__class__.__name__,
            "config_count": len(self.configs),
            "configs": {name: _entry(config) for name, config in self.configs.items()},
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
    """문자열/리스트 → 정수 리스트.

    Corruption 회복: 입력이 list 인데 element 가 escape 된 list-ish 문자열
    (예: `[ "[\\\"1\\\", \\\"2\\\"]" ]`) 인 경우 한 단계 풀어내 재시도.
    매 boot 마다 escape 누적으로 폭증하는 corruption 패턴을 차단한다.
    """
    if isinstance(value, list):
        # ── element-level escape 복원 ──
        # 단일 element 가 "[...]" 형태이면 그 안에 진짜 list 가 있다고 가정.
        if len(value) == 1 and isinstance(value[0], str):
            inner = value[0].strip()
            if inner.startswith('[') and inner.endswith(']'):
                try:
                    parsed = json.loads(inner)
                    if isinstance(parsed, list):
                        # 재귀 — element 가 또 escape string 일 수 있음.
                        return convert_to_int_list(parsed, separator)
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
        result = []
        for item in value:
            try:
                result.append(int(item))
            except (ValueError, TypeError):
                # element 가 또 escape string 일 수 있음 — 풀어보고 재귀.
                if isinstance(item, str):
                    s = item.strip()
                    if s.startswith('[') and s.endswith(']'):
                        result.extend(convert_to_int_list(s, separator))
        return result
    elif isinstance(value, str):
        value = value.strip()
        if value.startswith('[') and value.endswith(']'):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    # 재귀로 element-level corruption 도 처리.
                    return convert_to_int_list(parsed, separator)
                # parsed 가 string 이면 다중 escape — 한 번 더 시도.
                if isinstance(parsed, str):
                    return convert_to_int_list(parsed, separator)
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
