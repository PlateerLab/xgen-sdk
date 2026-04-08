"""
xgen_sdk.config.config_composer — 설정 통합 관리 (등록 + 사용)

sub_config/ 디렉토리에서 *_config.py 파일들을 자동 발견하여
BaseConfig 기반 설정 카테고리를 동적으로 로드합니다.

xgen-core: ConfigComposer로 등록 + 사용
xgen-workflow/documents: ConfigClient로 사용만

Usage:
    from xgen_sdk.config import ConfigComposer

    composer = ConfigComposer(
        redis_manager=xgen.config,
        db_manager=db_manager,
        sub_config_dir="/path/to/sub_config"
    )
    composer.ensure_redis_sync()

    # 이후 config 접근
    openai_key = composer.openai.API_KEY.value
"""
import importlib
import logging
from typing import Dict, Any, Optional
from pathlib import Path
from xgen_sdk.config.base_config import BaseConfig, PersistentConfig
from xgen_sdk.config.local_config import create_config_manager

logger = logging.getLogger("xgen-sdk.config-composer")


class ConfigComposer:
    """
    설정 통합 관리 클래스 — sub_config/ 자동 발견 + Redis/DB 동기화

    sub_config_dir를 지정하면 해당 디렉토리에서 *_config.py를 자동 스캔합니다.
    sub_config_module_prefix를 지정하면 해당 Python 모듈 경로로 import합니다.
    """

    def __init__(self, redis_manager=None, db_manager=None,
                 sub_config_dir: Optional[str] = None,
                 sub_config_module_prefix: str = "config.sub_config"):
        self.config_categories: Dict[str, Any] = {}
        self.all_configs: Dict[str, PersistentConfig] = {}
        self.redis_manager = redis_manager or create_config_manager(db_manager=db_manager)
        self.db_manager = db_manager
        self.logger = logger

        if sub_config_dir:
            self._discover_and_load_configs(
                Path(sub_config_dir),
                sub_config_module_prefix
            )

    def _discover_and_load_configs(self, sub_config_dir: Path,
                                    module_prefix: str):
        """sub_config/ 디렉토리에서 *_config.py 파일들을 자동 발견하고 로드"""
        if not sub_config_dir.exists():
            sub_config_dir.mkdir(parents=True, exist_ok=True)
            self.logger.warning("Created sub_config directory: %s", sub_config_dir)
            return

        config_files = [f for f in sub_config_dir.glob("*_config.py") if f.name != "__init__.py"]
        self.logger.info("Found %d config files: %s", len(config_files), [f.name for f in config_files])

        for config_file in config_files:
            try:
                category_name = config_file.stem.replace("_config", "")
                module_name = f"{module_prefix}.{config_file.stem}"
                module = importlib.import_module(module_name)

                # 클래스명 후보 생성
                possible_class_names = [
                    f"{category_name.title()}Config",
                    f"{category_name.upper()}Config",
                    f"{category_name.capitalize()}Config",
                    "".join(word.capitalize() for word in category_name.split("_")) + "Config"
                ]

                config_class = None
                for class_name in possible_class_names:
                    if hasattr(module, class_name):
                        config_class = getattr(module, class_name)
                        break

                if config_class is None:
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (isinstance(attr, type) and
                            issubclass(attr, BaseConfig) and
                            attr != BaseConfig):
                            config_class = attr
                            break

                if config_class is None:
                    raise AttributeError(f"No valid config class found in {module_name}")

                config_instance = config_class(
                    redis_manager=self.redis_manager,
                    db_manager=self.db_manager
                )

                self.config_categories[category_name] = config_instance
                setattr(self, category_name, config_instance)
                self.all_configs.update(config_instance.configs)

                self.logger.info("Loaded config category: %s", category_name)

            except Exception as e:
                self.logger.error("Failed to load config file %s: %s", config_file.name, e)

        self.logger.info("Auto-discovered %d config categories: %s",
                         len(self.config_categories), list(self.config_categories.keys()))

    def get_config_by_name(self, config_name: str) -> PersistentConfig:
        if config_name in self.all_configs:
            return self.all_configs[config_name]
        raise KeyError(f"Configuration '{config_name}' not found")

    def update_config(self, config_name: str, new_value: Any) -> Dict[str, Any]:
        config = self.get_config_by_name(config_name)
        old_value = config.value

        if config.type_converter:
            new_value = config.type_converter(new_value)
        config.value = new_value

        category = config.config_path.split('.')[0] if '.' in config.config_path else 'unknown'
        data_type = config._infer_data_type(new_value)

        self.redis_manager.set_config(
            config_path=config.config_path,
            config_value=new_value,
            data_type=data_type,
            category=category,
            env_name=config.env_name
        )

        self.logger.info("[Updated] [%s] %s | path=%s | %s → %s",
                         category, config.env_name, config.config_path, old_value, new_value)
        return {"old_value": old_value, "new_value": config.value}

    def refresh_all(self):
        for config_name, config in self.all_configs.items():
            try:
                config.refresh()
            except Exception as e:
                self.logger.error("Failed to refresh config %s: %s", config_name, e)

    def get_config_summary(self) -> Dict[str, Any]:
        try:
            categories_summary = {}
            all_configs_list = []

            for category_name, category_instance in self.config_categories.items():
                category_summary = category_instance.get_config_summary()
                categories_summary[category_name] = category_summary
                for env_name, config_obj in category_instance.configs.items():
                    all_configs_list.append({
                        "env_name": env_name,
                        "config_path": config_obj.config_path,
                        "current_value": config_obj.value,
                        "default_value": config_obj.env_value,
                        "is_saved": True
                    })

            return {
                "total_configs": len(all_configs_list),
                "discovered_categories": list(self.config_categories.keys()),
                "categories": categories_summary,
                "persistent_summary": {
                    "total_configs": len(all_configs_list),
                    "config_file": "constants/config.json",
                    "configs": all_configs_list
                }
            }
        except Exception as e:
            self.logger.error("Failed to get config summary: %s", e)
            return {
                "total_configs": 0,
                "discovered_categories": [],
                "categories": {},
                "persistent_summary": {"total_configs": 0, "config_file": "constants/config.json", "configs": []},
                "error": str(e)
            }

    def get_category_configs(self, category_name: str) -> Dict[str, Any]:
        if category_name not in self.config_categories:
            raise KeyError(f"Category '{category_name}' not found")
        category = self.config_categories[category_name]
        return {name: config.value for name, config in category.configs.items()}

    def ensure_redis_sync(self):
        """모든 설정이 Redis에 등록되어 있는지 확인, 없으면 복구"""
        self.logger.info("Redis sync check starting...")
        synced_count = 0
        for config_name, config in self.all_configs.items():
            if not self.redis_manager.exists(config.env_name):
                self.redis_manager.set_config(
                    config_path=config.config_path,
                    config_value=config.value,
                    data_type=config._infer_data_type(config.value),
                    env_name=config.env_name,
                    category=config.config_path.split('.')[0] if '.' in config.config_path else 'unknown'
                )
                synced_count += 1
        if synced_count > 0:
            self.logger.info("%d configs restored to Redis", synced_count)
        else:
            self.logger.info("All configs are synced with Redis")


# ============================================================== #
# 전역 싱글톤 (Lazy Initialization)
# ============================================================== #

_config_composer_instance = None


def get_config_composer() -> ConfigComposer:
    """ConfigComposer 싱글톤 반환 (Lazy Init)"""
    global _config_composer_instance
    if _config_composer_instance is None:
        logger.info("ConfigComposer instance initializing...")
        _config_composer_instance = ConfigComposer()
    return _config_composer_instance
