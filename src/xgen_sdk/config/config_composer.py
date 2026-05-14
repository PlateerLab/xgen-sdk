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
from xgen_sdk.config.base_config import (
    BaseConfig,
    PersistentConfig,
    _invalidate_version_cache,
    _read_version_cached,
)
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
        # Multi-Pod 캐시 인밸리데이션 — 본 Pod 가 "여기까지는 정합" 으로 인지한 version.
        # 본 Pod 자신의 write 직후 갱신 / refresh_all 직후 갱신.
        # refresh_all_if_stale 는 이 값과 Redis 의 현재 version 을 비교해 drift 감지.
        self._last_known_version: int = 0

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
        """특정 PersistentConfig 값을 갱신.

        Multi-Pod 안전 순서:
          1) Redis + DB 동기 쓰기. RedisConfigManager.set_config 가 내부적으로
             글로벌 version sentinel 을 atomic INCR 하고 INCR 반환값(=본 write
             직후의 정확한 version)을 `_last_write_version` 에 기록.
          2) 본 Pod 의 in-memory 캐시 (PersistentConfig._value) 갱신.
          3) `_last_write_version` 을 통해 본 write 의 정확한 version 을 캡처 —
             이는 "본 Pod 가 자기 자신의 write 직후 알게 된 정합 시점" 이며,
             다른 Pod 의 동시 쓰기가 있어 Redis 의 현재 version 이 더 크더라도
             그 차이는 _last_known_version < current 로 남아 다음 .value/
             refresh_all_if_stale 호출에서 정상적으로 drift 로 감지된다.
          4) 본 Pod 의 모든 PersistentConfig 의 _last_seen_version 을 위
             "본 write 시점" 으로 정렬해 단일 write 가 다른 키의 불필요한
             refresh 를 트리거하는 stampede 를 차단.

        다른 Pod 의 동시 쓰기 안전성:
          - C 가 A 의 write 사이에 INCR 했다면 Redis 현재 version > 본 _last_known.
          - 본 Pod 의 다음 refresh_all_if_stale 가 이를 감지해 refresh_all 수행.
          - 따라서 본 Pod 는 자기 write 직후 영구 stale 상태에 빠지지 않는다.
        """
        config = self.get_config_by_name(config_name)
        old_value = config.value

        if config.type_converter:
            new_value = config.type_converter(new_value)

        category = config.config_path.split('.')[0] if '.' in config.config_path else 'unknown'
        data_type = config._infer_data_type(new_value)

        # 1) 권위(Redis/DB) 부터 갱신. set_config 안에서 version INCR + _last_write_version 기록.
        self.redis_manager.set_config(
            config_path=config.config_path,
            config_value=new_value,
            data_type=data_type,
            category=category,
            env_name=config.env_name
        )

        # 다음 _read_version_cached 가 fresh 한 값을 읽도록 클래스 캐시 무효화.
        _invalidate_version_cache()

        # 2) 본 Pod 의 in-memory 캐시 갱신.
        config.value = new_value  # type 변환은 setter 가 다시 처리하지만 멱등.

        # 3) 본 write 의 정확한 version 캡처 — 다른 Pod 의 동시 INCR 에 영향 안 받음.
        my_write_version = int(getattr(self.redis_manager, '_last_write_version', 0) or 0)
        if my_write_version > 0:
            self._last_known_version = my_write_version
        else:
            # _last_write_version 미지원 / INCR 실패 시 best-effort 폴백.
            try:
                self._last_known_version = _read_version_cached(self.redis_manager)
            except Exception:
                pass

        # 4) 본 Pod 의 모든 config 의 sentinel 을 본 write 시점으로 정렬.
        try:
            for c in self.all_configs.values():
                c._last_seen_version = self._last_known_version
        except Exception as e:
            self.logger.debug("Failed to sync _last_seen_version after update: %s", e)

        self.logger.info("[Updated] [%s] %s | path=%s | %s → %s | version=%s",
                         category, config.env_name, config.config_path,
                         old_value, new_value, self._last_known_version)
        return {"old_value": old_value, "new_value": config.value}

    def refresh_all(self):
        """모든 PersistentConfig 를 DB/Redis 에서 강제 재로드 + sentinel 동기화.

        명시적 reload — version 변화 여부와 관계없이 항상 _load_value 를 돈다.
        cheap fast-path 가 필요하면 `refresh_all_if_stale()` 를 사용할 것.

        refresh 도중 default-restore 등으로 일부 config 의 _last_seen_version 이
        서로 어긋날 수 있으므로, 마지막에 모든 sentinel + composer 의
        _last_known_version 을 동일 값으로 정렬한다 — 이후 refresh_all_if_stale
        가 stampede 로 빠지지 않도록.
        """
        # version 캐시를 비워서 refresh 직후 sentinel 이 fresh 한 값을 잡도록.
        _invalidate_version_cache()
        for config_name, config in self.all_configs.items():
            try:
                config.refresh()
            except Exception as e:
                self.logger.error("Failed to refresh config %s: %s", config_name, e)
        # 최종 sentinel 정렬 — refresh 도중 발생한 모든 version bump 흡수.
        try:
            _invalidate_version_cache()
            final_version = _read_version_cached(self.redis_manager)
            self._last_known_version = final_version
            for c in self.all_configs.values():
                c._last_seen_version = final_version
        except Exception as e:
            self.logger.debug("Failed to align sentinels after refresh_all: %s", e)

    def refresh_all_if_stale(self) -> bool:
        """글로벌 version sentinel 이 본 Pod 의 마지막 관측치와 다르면 전체 refresh.

        Multi-Pod 환경의 안전망 — GET 핸들러 등 "응답 전에 정합을 보장하고 싶다"
        는 시점에서 호출. 비용은 Redis GET 1회 (50ms TTL 메모이즈됨) 이며 변화가
        없으면 fast-path 로 즉시 반환.

        본 Pod 가 자기 자신의 write 직후라도 다른 Pod 의 동시 INCR 로 Redis 의
        현재 version > _last_known_version 이 되면 여기서 drift 가 감지돼 refresh
        된다 (own-write race 회복).

        Returns:
            실제 refresh 가 수행됐는지 여부. (디버깅/관찰용)
        """
        try:
            current = _read_version_cached(self.redis_manager)
            if current != self._last_known_version:
                self.logger.info(
                    "Config version drift detected (last_known=%s current=%s) — refreshing all",
                    self._last_known_version, current
                )
                self.refresh_all()
                return True
            return False
        except Exception as e:
            self.logger.warning("refresh_all_if_stale failed (graceful skip): %s", e)
            return False

    def get_config_summary(self) -> Dict[str, Any]:
        try:
            categories_summary = {}
            all_configs_list = []

            for category_name, category_instance in self.config_categories.items():
                category_summary = category_instance.get_config_summary()
                categories_summary[category_name] = category_summary
                for env_name, config_obj in category_instance.configs.items():
                    entry = {
                        "env_name": env_name,
                        "config_path": config_obj.config_path,
                        "current_value": config_obj.value,
                        "default_value": config_obj.env_value,
                        "is_saved": True,
                    }
                    # UI 메타데이터 (선택). 프론트가 셀렉터/도움말 렌더에 사용.
                    if getattr(config_obj, 'options', None):
                        entry["options"] = list(config_obj.options)
                    if getattr(config_obj, 'description', None):
                        entry["description"] = config_obj.description
                    if getattr(config_obj, 'label', None):
                        entry["label"] = config_obj.label
                    all_configs_list.append(entry)

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
        """모든 설정이 Redis에 등록되어 있는지 확인, 없으면 복구.

        Redis 에 부재한 키를 set_config 로 복구하면 그 과정에서 version sentinel 이
        bump 된다. 이대로 두면 본 Pod 의 PersistentConfig 들이 "version 변경됨" 으로
        오인식해 다음 .value 접근마다 reload 가 일어나는 stampede 가 발생한다.
        sync 종료 후 모든 sentinel 을 현재 version 으로 정렬해 이를 방지한다.
        """
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

        # boot stampede 방지 — sync 직후 sentinel + composer._last_known_version 을
        # 현재 version 으로 정렬. 이후 다른 Pod 의 write 가 발생하면 즉시 drift 감지.
        try:
            _invalidate_version_cache()
            current_version = _read_version_cached(self.redis_manager)
            self._last_known_version = current_version
            for c in self.all_configs.values():
                c._last_seen_version = current_version
            self.logger.info("Config version sentinel initialized to %s", current_version)
        except Exception as e:
            self.logger.debug("Failed to align version sentinel after ensure_redis_sync: %s", e)


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
