"""
Permission 유틸리티

권한 정의는 각 엔드포인트의 require_perm() 데코레이터에서 수행됨.
이 모듈은 레지스트리에 수집된 권한 정보를 조회하는 헬퍼 함수만 제공.

구조:
    1) 엔드포인트에서 require_perm("admin.role:read", description="역할 조회") 적용
    2) SDK 레지스트리가 자동 수집
    3) 서버 시작 시 validate_and_sync()로 DB 동기화
    4) 프론트엔드는 DB에서 권한 목록 조회하여 역할에 할당

사용법:
    from xgen_sdk.auth import require_perm, permissions_grouped
    from fastapi import Depends

    @router.get("/roles")
    async def get_roles(request: Request,
                        session=Depends(require_perm("admin.role:read",
                                                     description="역할 조회"))):
        ...

    # 등록된 전체 권한 조회 (프론트엔드 API 등):
    grouped = permissions_grouped()
"""


# ─────────────────────────────────────────────────────────
# 유틸리티 함수 (레지스트리에서 읽기)
# ─────────────────────────────────────────────────────────

def permission_string(resource: str, action: str) -> str:
    """resource:action 형태의 permission 문자열 생성"""
    return f"{resource}:{action}"


def all_permission_strings() -> list:
    """레지스트리에 등록된 전체 permission 문자열 목록"""
    from xgen_sdk.auth.permission_registry import registry
    return sorted(registry.all_permission_keys())


def permissions_by_resource(resource: str) -> list:
    """특정 resource의 permission 문자열 목록"""
    from xgen_sdk.auth.permission_registry import registry
    return sorted(
        key for key in registry.all_permission_keys()
        if key.startswith(f"{resource}:")
    )


def permissions_grouped() -> dict:
    """resource별로 그룹핑된 permission 사전

    Returns:
        {
            "admin.role": [("read", "역할 조회"), ("manage", "역할 관리")],
            "admin.user": [("create", "사용자 생성"), ...],
            ...
        }
    """
    from xgen_sdk.auth.permission_registry import registry
    grouped = {}
    for key, info in registry.all_permissions().items():
        resource = info["resource"]
        action = info["action"]
        description = info.get("description", "")
        if resource not in grouped:
            grouped[resource] = []
        grouped[resource].append((action, description))
    return grouped
