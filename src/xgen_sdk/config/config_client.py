"""
xgen_sdk.config.config_client — High-level Config Client

RedisConfigManager를 래핑하여 PersistentConfig(.value) 패턴을 제공합니다.
xgen-workflow, xgen-documents 등에서 사용하는 표준 Config 인터페이스입니다.

Usage:
    from xgen_sdk.config import ConfigClient

    config = ConfigClient()
    value = config.get_config_by_name("OPENAI_API_KEY").value

    category = config.get_config_by_category_name("llm")
    model = category.OPENAI_MODEL.value
"""

import logging
import threading
from typing import Dict, Any, Optional, List, Generic, TypeVar

logger = logging.getLogger("xgen-sdk.config-client")

T = TypeVar('T')


# ============================================================== #
# PersistentConfig
# ============================================================== #

class PersistentConfig(Generic[T]):
    """설정 값을 담는 데이터 컨테이너 (.value 접근 패턴 제공)"""

    def __init__(
        self,
        env_name: str,
        config_path: str,
        value: T,
        env_value: T = None,
        config_value: T = None,
        data_type: str = "string",
        category: str = None,
        status: str = "ok",
        source: str = "",
    ):
        self.env_name = env_name
        self.config_path = config_path
        self.value = value
        #: 조회 결과 상태 — "ok" | "missing" | "error".
        #: ``error`` 는 **"설정 안 함" 이 아니라 "읽지 못했다"** 이다. 보호 여부를
        #: 이 값으로 정하는 호출자는 반드시 구분해야 한다(fail-open 방지).
        self.status = status
        #: 값을 실제로 읽어 온 곳 — "redis" | "db" | "memory" | "".
        self.source = source
        self.env_value = env_value if env_value is not None else value
        self.config_value = config_value
        self.data_type = data_type
        self.category = category

    def __str__(self):
        return str(self.value)

    def __repr__(self):
        return f"PersistentConfig(env_name='{self.env_name}', value={self.value})"

    def refresh(self):
        """설정 새로고침 (no-op — 재조회 시 새 인스턴스 생성)"""
        pass

    def to_dict(self) -> Dict[str, Any]:
        return {
            "env_name": self.env_name,
            "path": self.config_path,
            "value": self.value,
            "type": self.data_type,
            "category": self.category,
            "config_value": self.config_value,
            "env_value": self.env_value,
        }


# ============================================================== #
# DynamicCategoryConfig
# ============================================================== #

class DynamicCategoryConfig:
    """동적 카테고리 설정 — 각 설정을 속성으로 접근 가능"""

    def __init__(self, category_name: str):
        self.category_name = category_name
        self.configs: Dict[str, PersistentConfig] = {}

    def __repr__(self):
        return f"DynamicCategoryConfig(category='{self.category_name}', configs={list(self.configs.keys())})"

    def __getattribute__(self, item):
        if item in ("category_name", "configs", "__dict__", "__class__", "__repr__", "get_all_configs"):
            return super().__getattribute__(item)
        configs = super().__getattribute__("configs")
        if item in configs:
            return configs[item]
        return super().__getattribute__(item)

    def get_all_configs(self) -> Dict[str, PersistentConfig]:
        return self.configs


# ============================================================== #
# ConfigClient
# ============================================================== #

class ConfigClient:
    """
    SDK 기반 Config 클라이언트.

    내부적으로 RedisConfigManager(또는 LocalConfigManager)를 사용하며
    PersistentConfig 객체 (.value 접근) 패턴을 제공합니다.
    """

    def __init__(self, manager=None, db_manager=None):
        """
        Args:
            manager: 사용할 config manager 인스턴스.
                     None이면 create_config_manager()로 자동 생성.
            db_manager: 영속 저장소(persistent_configs) 매니저. 주면 Redis 가
                비어 있거나 죽었을 때 **DB 로 폴백**한다 — core 와 같은 사다리.

        ⚠ db_manager 없이 만들면 이 클라이언트는 **Redis 만** 본다. 그 상태가
        오래 문제였다: 관리자 화면(core)은 DB 까지 보니 [설정됨] 인데, 위성 파드는
        Redis 에서 키가 빠진 순간 빈 값을 보고 그대로 실행했다. 앱 기동 순서 때문에
        생성 시점에 DB 가 아직 없다면 :meth:`attach_db_manager` 로 나중에 붙인다.
        """
        if manager is not None:
            self._manager = manager
        else:
            from xgen_sdk.config import create_config_manager
            self._manager = create_config_manager(db_manager=db_manager)
        if db_manager is not None and getattr(self._manager, "db_manager", None) is None:
            self._manager.db_manager = db_manager
        logger.info(
            "ConfigClient initialized (%s, db_fallback=%s)",
            type(self._manager).__name__,
            getattr(self._manager, "db_manager", None) is not None,
        )

    def attach_db_manager(self, db_manager) -> bool:
        """DB 폴백을 나중에 붙인다 — 앱이 config 를 먼저 만들고 DB 를 나중에 여는 순서용.

        붙이고 나면 이 클라이언트의 모든 조회가 core 와 **같은 사다리**
        (Redis → DB)를 탄다. 이미 붙어 있으면 아무것도 하지 않는다.
        """
        if db_manager is None:
            return False
        if getattr(self._manager, "db_manager", None) is not None:
            return False
        self._manager.db_manager = db_manager
        # 붙기 전에 "DB 에도 없다" 고 배운 이름들을 버린다 — 그대로 두면 DB 를
        # 붙여 놓고도 그 이름만 계속 없는 것으로 보인다.
        clear = getattr(self._manager, "clear_db_miss_memo", None)
        if callable(clear):
            clear()
        # 메모리 매니저(Redis 없는 배포)는 붙는 순간 DB 를 한 번 읽어 채운다 —
        # 그러지 않으면 "DB 는 붙었는데 값은 여전히 비어 있는" 상태가 남는다.
        loader = getattr(self._manager, "_load_from_db", None)
        if callable(loader):
            try:
                loader()
            except Exception as e:  # noqa: BLE001
                logger.warning("ConfigClient: DB 적재 실패: %s", e)
        logger.info("ConfigClient: DB 폴백 연결됨 (%s)", type(db_manager).__name__)
        return True

    @property
    def has_db_fallback(self) -> bool:
        return getattr(self._manager, "db_manager", None) is not None

    # ========== Core Methods ==========

    # ========== 범용 캐시 version sentinel ==========

    def get_cache_version(self, namespace: str) -> int:
        """네임스페이스의 캐시 버전. 0 = 버전 정보 없음(호출처는 TTL 로 버틴다)."""
        try:
            return int(self._manager.get_cache_version(namespace))
        except Exception:  # noqa: BLE001 — 캐시 힌트가 요청을 실패시키면 안 된다
            return 0

    def bump_cache_version(self, namespace: str) -> int:
        """쓰기 경로에서 부른다 — 다른 Pod 의 캐시를 무효화한다."""
        try:
            return int(self._manager.bump_cache_version(namespace))
        except Exception:  # noqa: BLE001
            return 0

    def health_check(self) -> bool:
        return self._manager.health_check()

    def get_config_value(self, env_name: str, default: Any = None) -> Any:
        return self._manager.get_config_value(env_name, default)

    def probe_config_value(self, env_name: str):
        """설정 값 조회 — (상태, 값). "못 읽었다" 를 숨기지 않는다 (1.39.0).

        ("ok", value) | ("missing", None) | ("error", None). 저장소 장애를 "설정 안 함"
        으로 오독해 보호가 풀리는 것(fail-open)을 막아야 하는 호출자가 쓴다.
        """
        probe = getattr(self._manager, "probe_config_value", None)
        if callable(probe):
            return probe(env_name)
        # 구버전 매니저 호환 — 상태를 알 수 없으므로 health_check 로 가른다.
        check = getattr(self._manager, "health_check", None)
        if callable(check) and not check():
            return ("error", None)
        value = self._manager.get_config_value(env_name, None)
        return ("missing", None) if value is None else ("ok", value)

    def get_config(self, env_name: str) -> Optional[Dict[str, Any]]:
        return self._manager.get_config(env_name)

    def set_config(
        self,
        config_path: str,
        config_value: Any,
        data_type: str = "string",
        category: Optional[str] = None,
        env_name: Optional[str] = None,
    ) -> bool:
        return self._manager.set_config(config_path, config_value, data_type, category, env_name)

    def update_config(
        self,
        config_path: str,
        config_value: Any,
        data_type: str = "string",
        category: Optional[str] = None,
        env_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._manager.update_config(config_path, config_value)
        return {"result": "success", "config_path": config_path}

    def delete_config(self, env_name: str) -> bool:
        return self._manager.delete_config(env_name)

    def exists(self, env_name: str) -> bool:
        return self._manager.exists(env_name)

    # ========== Category Methods ==========

    def get_category_configs(self, category: str) -> List[Dict[str, Any]]:
        return self._manager.get_category_configs(category)

    def get_category_configs_nested(self, category: str) -> Dict[str, Any]:
        return self._manager.get_category_configs_nested(category)

    def get_all_categories(self) -> List[str]:
        return self._manager.get_all_categories()

    def get_all_configs_by_category(self, category: str) -> List[Dict[str, Any]]:
        return self._manager.get_category_configs(category)

    def get_all_configs(self) -> List[Dict[str, Any]]:
        return self._manager.get_all_configs()

    # ========== PersistentConfig 패턴 ==========

    def get_config_by_name(self, config_name: str) -> PersistentConfig:
        """이름으로 설정 조회 — PersistentConfig 객체 반환 (.value 접근 가능).

        Redis → DB 사다리를 탄다(DB 폴백이 붙어 있을 때). 결과에는 ``.status`` 와
        ``.source`` 가 함께 담긴다.

        ⚠ 예전에는 ``except (KeyError, Exception)`` 로 **모든 실패를 value=None 으로**
        접어 버렸다. 그래서 "설정 안 함" 과 "저장소를 못 읽었다" 가 호출부에서
        구분되지 않았고, 후자가 조용히 전자처럼 동작했다 — 빈 API 키가 그대로
        provider SDK 로 들어가는 사고가 여기서 났다. 이제 못 읽으면 로그가 그렇게
        말하고 ``status="error"`` 가 붙는다.
        """
        status, value, source = self.probe_config_status(config_name)
        if status == "error":
            logger.warning(
                "config 조회 실패(저장소 응답 없음): %s — 값 없음으로 취급되지만 "
                "'설정 안 함' 이 아니다", config_name,
            )
        return PersistentConfig(
            env_name=config_name,
            config_path=config_name,
            value=value,
            status=status,
            source=source,
        )

    def probe_config_status(self, config_name: str):
        """(상태, 값, 출처) — 매니저의 공통 사다리 결과를 그대로 돌려준다."""
        probe = getattr(self._manager, "probe_value", None)
        if callable(probe):
            try:
                return probe(config_name, config_name)
            except Exception as e:  # noqa: BLE001
                logger.warning("probe_value 실패(%s): %s", config_name, e)
                return ("error", None, "")
        try:
            return ("ok", self._manager.get_config_by_name(config_name), "")
        except KeyError:
            return ("missing", None, "")
        except Exception as e:  # noqa: BLE001
            logger.warning("config 조회 실패(%s): %s", config_name, e)
            return ("error", None, "")

    def get_config_by_category_name(self, category_name: str) -> DynamicCategoryConfig:
        """카테고리별 설정 조회 — DynamicCategoryConfig 객체 반환"""
        dynamic = DynamicCategoryConfig(category_name)
        try:
            configs = self._manager.get_category_configs(category_name)
            for cfg in configs:
                env_name = cfg.get("env_name", cfg.get("path", ""))
                pc = PersistentConfig(
                    env_name=env_name,
                    config_path=cfg.get("path", ""),
                    value=cfg.get("value"),
                    data_type=cfg.get("type", "string"),
                    category=category_name,
                )
                key = env_name.split(".")[-1] if "." in env_name else env_name
                dynamic.configs[key] = pc
        except Exception as e:
            logger.warning("카테고리 설정 로드 실패: %s - %s", category_name, e)
        return dynamic

    def get_all_config(self, **kwargs) -> Dict[str, Any]:
        return self._manager.get_all_config(**kwargs)

    def get_config_summary(self) -> Dict[str, Any]:
        return self._manager.get_config_summary()

    def refresh_all(self) -> None:
        self._manager.refresh_all()

    def get_config_version(self) -> int:
        """글로벌 config version sentinel 값. Manager 호환 메서드 위임."""
        try:
            return self._manager.get_config_version()
        except Exception:
            return 0

    def bump_meta_version(self) -> int:
        """글로벌 version sentinel 을 명시적으로 INCR — 클라이언트 인스턴스 재구성용.

        Config 값 자체를 바꾸지 않고 각 Pod 의 캐시된 인스턴스(GuarderClient 등)
        를 강제로 재빌드하도록 유도한다. Multi-pod 환경에서 'reset' 동작에 사용.
        """
        try:
            return self._manager.bump_meta_version()
        except Exception as e:
            logger.warning("bump_meta_version delegation failed: %s", e)
            return 0

    def export_config_summary(self) -> Dict[str, Any]:
        return self._manager.export_config_summary()

    def get_registry_statistics(self) -> Dict[str, Any]:
        return self._manager.get_registry_statistics()

    # ========== PersistentConfig 변환 ==========

    def get_all_persistent_configs(self) -> List[PersistentConfig]:
        configs = self._manager.get_all_configs()
        return [
            PersistentConfig(
                env_name=cfg.get("env_name", cfg.get("path", "")),
                config_path=cfg.get("path", ""),
                value=cfg.get("value"),
                data_type=cfg.get("type", "string"),
                category=cfg.get("category"),
            )
            for cfg in configs
        ]

    def get_category_persistent_configs(self, category: str) -> List[PersistentConfig]:
        configs = self._manager.get_category_configs(category)
        return [
            PersistentConfig(
                env_name=cfg.get("env_name", cfg.get("path", "")),
                config_path=cfg.get("path", ""),
                value=cfg.get("value"),
                data_type=cfg.get("type", "string"),
                category=category,
            )
            for cfg in configs
        ]

    def refresh_all_configs(self):
        self._manager.refresh_all()

    # ========== Lifecycle ==========

    def close(self):
        if hasattr(self._manager, 'close'):
            self._manager.close()
        logger.info("ConfigClient closed")


# ============================================================== #
# 프로세스 하나짜리 클라이언트
#
# 여러 모듈이 각자 ConfigClient() 를 만들면, 그중 **DB 폴백이 붙은 것과 안 붙은
# 것이 섞인다**. 같은 파드 안에서도 어떤 조회는 Redis 만 보고 어떤 조회는 DB 까지
# 보는 상태가 되고, 값이 어긋난 날 어느 경로였는지 아무도 되짚지 못한다.
# 읽는 길이 하나여야 한다 — 그 하나를 여기서 준다.
# ============================================================== #

_client_lock = threading.Lock()
_client_singleton: Optional["ConfigClient"] = None


def get_config_client(db_manager=None, manager=None) -> "ConfigClient":
    """이 프로세스의 ConfigClient. 없으면 만들고, ``db_manager`` 를 주면 붙인다.

    앱 기동 순서상 DB 가 늦게 열리는 곳이 많아서, 나중에 다시 부르며 DB 를
    붙이는 것을 정상 경로로 둔다 (이미 붙어 있으면 무시).
    """
    global _client_singleton
    with _client_lock:
        if _client_singleton is None:
            _client_singleton = ConfigClient(manager=manager, db_manager=db_manager)
        elif db_manager is not None:
            _client_singleton.attach_db_manager(db_manager)
        return _client_singleton


def set_config_client(client: "ConfigClient") -> None:
    """앱이 이미 만든 클라이언트를 이 프로세스의 정본으로 등록한다."""
    global _client_singleton
    with _client_lock:
        _client_singleton = client


def reset_config_client() -> None:
    """싱글톤 폐기 (테스트·헬스체커의 재생성 경로용)."""
    global _client_singleton
    with _client_lock:
        _client_singleton = None
