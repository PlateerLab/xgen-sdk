"""
xgen_sdk.auth — 통합 인증/인가 모듈

Gateway 헤더 파싱, 권한 레지스트리/리졸버, 감독 범위 해석

사용 패턴 (엔드포인트가 곧 권한 정의):
    from xgen_sdk.auth import require_perm
    from fastapi import Depends

    @router.get("/roles")
    async def get_roles(request: Request,
                        session=Depends(require_perm("admin.role:read",
                                                     description="역할 조회"))):
        ...

    # 데코레이터 적용만으로:
    #   1) 레지스트리에 자동 등록
    #   2) 요청 시 권한 체크 (superuser 자동 통과)
    #   3) 서버 시작 시 validate_and_sync()로 DB 동기화
    #   4) 프론트엔드는 DB에서 권한 목록을 읽어 역할에 할당
"""

from xgen_sdk.auth.gateway import (
    get_user_info_by_gateway,
    normalize_user_id,
    require_permission,
    require_any_permission,
    require_superuser,
    extract_user_id,
)
from xgen_sdk.auth.principal import (
    PrincipalConfigurationError,
    PrincipalMiddleware,
    PrincipalValidationError,
    get_verified_principal_headers,
    service_principal_headers,
    sign_principal_for_service,
)
from xgen_sdk.auth.document_capability import (
    DOCUMENT_CAPABILITY_AUDIENCE,
    DOCUMENT_CAPABILITY_ISSUER,
    DOCUMENT_CAPABILITY_KEYS_ENV,
    DOCUMENT_CAPABILITY_OPERATIONS,
    DOCUMENT_CAPABILITY_TYPE,
    DocumentCapabilityConfigurationError,
    DocumentCapabilityValidationError,
    sign_document_capability,
    verify_document_capability,
)
from xgen_sdk.auth.deployment_policy import (
    DEPLOYMENT_POLICY_FIELDS,
    DeploymentPolicyDigestError,
    deployment_policy_digest,
)
from xgen_sdk.auth.permission_constants import (
    permission_string,
    all_permission_strings,
    permissions_by_resource,
    permissions_grouped,
)
from xgen_sdk.auth.permission_registry import (
    registry,
    require_perm,
    require_any_perm,
    require_login,
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
)
from xgen_sdk.auth.object_guard import (
    assert_owns_execution_io,
    assert_owns_feedback,
    assert_owns_test_run,
    assert_owns_workflow,
)

__all__ = [
    # gateway
    "get_user_info_by_gateway",
    "normalize_user_id",
    "require_permission",
    "require_any_permission",
    "require_superuser",
    "extract_user_id",
    "PrincipalConfigurationError",
    "PrincipalMiddleware",
    "PrincipalValidationError",
    "get_verified_principal_headers",
    "service_principal_headers",
    "sign_principal_for_service",
    "DOCUMENT_CAPABILITY_AUDIENCE",
    "DOCUMENT_CAPABILITY_ISSUER",
    "DOCUMENT_CAPABILITY_KEYS_ENV",
    "DOCUMENT_CAPABILITY_OPERATIONS",
    "DOCUMENT_CAPABILITY_TYPE",
    "DocumentCapabilityConfigurationError",
    "DocumentCapabilityValidationError",
    "sign_document_capability",
    "verify_document_capability",
    "DEPLOYMENT_POLICY_FIELDS",
    "DeploymentPolicyDigestError",
    "deployment_policy_digest",
    # constants (utility)
    "permission_string",
    "all_permission_strings",
    "permissions_by_resource",
    "permissions_grouped",
    # registry
    "registry",
    "require_perm",
    "require_any_perm",
    "require_login",
    "validate_and_sync",
    # resolver
    "resolve_user_permissions",
    "has_permission",
    "get_user_roles",
    # supervision
    "resolve_supervision_scope",
    "resolve_supervised_user_ids",
    # object guard
    "assert_owns_execution_io",
    "assert_owns_feedback",
    "assert_owns_test_run",
    "assert_owns_workflow",
]
