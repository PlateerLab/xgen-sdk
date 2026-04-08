"""
xgen_sdk.auth — 통합 인증/인가 모듈

Gateway 헤더 파싱, ABAC 권한 상수/레지스트리/리졸버, 감독 범위 해석
"""

from xgen_sdk.auth.gateway import (
    get_user_info_by_gateway,
    normalize_user_id,
)
from xgen_sdk.auth.permission_constants import (
    ALL_PERMISSIONS,
    permission_string,
    all_permission_strings,
    permissions_by_resource,
    permissions_grouped,
)
from xgen_sdk.auth.permission_registry import (
    registry,
    load_constants,
    require_perm,
    validate_and_sync,
)
from xgen_sdk.auth.permission_resolver import (
    resolve_user_permissions,
    has_permission,
    get_user_roles,
)
from xgen_sdk.auth.supervision_resolver import (
    resolve_supervision_scope,
    resolve_supervised_user_ids,
    can_supervise_user,
)

__all__ = [
    # gateway
    "get_user_info_by_gateway",
    "normalize_user_id",
    # constants
    "ALL_PERMISSIONS",
    "permission_string",
    "all_permission_strings",
    "permissions_by_resource",
    "permissions_grouped",
    # registry
    "registry",
    "load_constants",
    "require_perm",
    "validate_and_sync",
    # resolver
    "resolve_user_permissions",
    "has_permission",
    "get_user_roles",
    # supervision
    "resolve_supervision_scope",
    "resolve_supervised_user_ids",
    "can_supervise_user",
]
