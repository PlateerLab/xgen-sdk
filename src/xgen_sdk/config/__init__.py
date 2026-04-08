"""
xgen_sdk.config — 통합 Config 관리 모듈

등록(BaseConfig + ConfigComposer) + 사용(ConfigClient) 모두 SDK에서 제공.

xgen-core: ConfigComposer로 sub_config 등록 + 사용
xgen-workflow/documents: ConfigClient로 사용만
"""

from xgen_sdk.config.redis_config import RedisConfigManager
from xgen_sdk.config.local_config import LocalConfigManager, create_config_manager
from xgen_sdk.config.config_client import ConfigClient, DynamicCategoryConfig
from xgen_sdk.config.base_config import (
    BaseConfig,
    PersistentConfig,
    convert_to_str,
    convert_to_int,
    convert_to_float,
    convert_to_bool,
    convert_to_list,
    convert_to_int_list,
)
from xgen_sdk.config.config_composer import ConfigComposer, get_config_composer
from xgen_sdk.config.config_utils import (
    dict_to_namespace,
    get_config_dict,
    get_category_config,
    get_flat_config,
    get_config_value,
    get_multiple_configs,
    get_all_categories,
    update_config,
    get_app_config,
    get_openai_config,
    get_anthropic_config,
    get_vast_config,
    get_vllm_config,
)

__all__ = [
    # 저장소
    "RedisConfigManager",
    "LocalConfigManager",
    "create_config_manager",
    # 등록 (xgen-core)
    "BaseConfig",
    "PersistentConfig",
    "ConfigComposer",
    "get_config_composer",
    # 타입 변환
    "convert_to_str",
    "convert_to_int",
    "convert_to_float",
    "convert_to_bool",
    "convert_to_list",
    "convert_to_int_list",
    # 사용 (workflow/documents)
    "ConfigClient",
    "DynamicCategoryConfig",
    # 유틸리티
    "dict_to_namespace",
    "get_config_dict",
    "get_category_config",
    "get_flat_config",
    "get_config_value",
    "get_multiple_configs",
    "get_all_categories",
    "update_config",
    "get_app_config",
    "get_openai_config",
    "get_anthropic_config",
    "get_vast_config",
    "get_vllm_config",
]
