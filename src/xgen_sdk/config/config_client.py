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
    ):
        self.env_name = env_name
        self.config_path = config_path
        self.value = value
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

    def __init__(self, manager=None):
        """
        Args:
            manager: 사용할 config manager 인스턴스.
                     None이면 create_config_manager()로 자동 생성.
        """
        if manager is not None:
            self._manager = manager
        else:
            from xgen_sdk.config import create_config_manager
            self._manager = create_config_manager(db_manager=None)
        logger.info("ConfigClient initialized (%s)", type(self._manager).__name__)

    # ========== Core Methods ==========

    def health_check(self) -> bool:
        return self._manager.health_check()

    def get_config_value(self, env_name: str, default: Any = None) -> Any:
        return self._manager.get_config_value(env_name, default)

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
        """이름으로 설정 조회 — PersistentConfig 객체 반환 (.value 접근 가능)"""
        try:
            value = self._manager.get_config_by_name(config_name)
            return PersistentConfig(
                env_name=config_name,
                config_path=config_name,
                value=value,
            )
        except (KeyError, Exception):
            return PersistentConfig(
                env_name=config_name,
                config_path=config_name,
                value=None,
            )

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
