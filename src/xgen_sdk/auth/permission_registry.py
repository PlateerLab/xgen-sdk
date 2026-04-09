"""
Permission Registry — 데코레이터 기반 권한 자동 수집 + DB 동기화

엔드포인트에서 require_perm() 데코레이터를 적용하면:
    1) import 시점에 레지스트리에 자동 등록
    2) 요청 시점에 권한 체크
    3) 서버 시작 시 validate_and_sync()로 DB 동기화

이렇게 하면:
    - 새 기능 추가: 엔드포인트에 require_perm("xxx:yyy", description="...") 만 달면 끝
    - 기능 제거: 데코레이터 제거 → 시작 시 orphan 감지
    - 별도 상수 관리 불필요: 엔드포인트가 곧 권한 정의
"""
import logging
from typing import Set, Dict, List, Optional
from fastapi import Request, HTTPException
from xgen_sdk.db.base_model import BaseModel as _SdkBaseModel


# ─────────────────────────────────────────────────────────
# SDK 내장 Permission 모델 (외부 서비스에서도 validate_and_sync 가능)
# ─────────────────────────────────────────────────────────

class _PermissionModel(_SdkBaseModel):
    """SDK 내장 경량 Permission 모델.

    xgen-core의 Permission 모델과 동일 스키마.
    validate_and_sync() 에서 permission_model_class 를 생략하면 이 모델이 사용된다.
    덕분에 xgen-workflow, xgen-documents 등 외부 서비스에서도
    xgen-core의 모델을 import 하지 않고 독립적으로 권한 동기화가 가능하다.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.resource: str = kwargs.get('resource', '')
        self.action: str = kwargs.get('action', '')
        self.description: Optional[str] = kwargs.get('description')

    def get_table_name(self) -> str:
        return "permissions"

    def get_schema(self) -> Dict[str, str]:
        return {
            'resource': 'VARCHAR(100) NOT NULL',
            'action': 'VARCHAR(50) NOT NULL',
            'description': 'TEXT',
            'UNIQUE_resource_action': 'UNIQUE(resource, action)',
        }

    def get_indexes(self) -> List[tuple]:
        return [
            ("idx_permissions_resource", "resource"),
            ("idx_permissions_resource_action", "resource, action"),
        ]

logger = logging.getLogger("permission-registry")


# ─────────────────────────────────────────────────────────
# Global Registry
# ─────────────────────────────────────────────────────────

class _PermissionRegistry:
    """싱글톤 권한 레지스트리.

    require_perm() 호출 시점(import 시점)에 자동으로 권한이 등록된다.
    """

    def __init__(self):
        # { "admin.user:read": {"resource": "admin.user", "action": "read", "description": "사용자 조회"} }
        self._permissions: Dict[str, Dict] = {}

    def register(self, perm_key: str, description: str = ""):
        """권한을 레지스트리에 등록.

        동일 권한이 여러 엔드포인트에서 등록되면 첫 번째 description을 유지.
        """
        parts = perm_key.split(":")
        if len(parts) != 2:
            logger.warning(f"Invalid permission format (expected 'resource:action'): {perm_key}")
            return

        resource, action = parts
        if perm_key not in self._permissions:
            self._permissions[perm_key] = {
                "resource": resource,
                "action": action,
                "description": description,
            }
        elif description and not self._permissions[perm_key]["description"]:
            self._permissions[perm_key]["description"] = description

    def all_permissions(self) -> Dict[str, Dict]:
        """등록된 전체 권한 사전."""
        return dict(self._permissions)

    def all_permission_keys(self) -> Set[str]:
        """등록된 전체 권한 문자열 집합."""
        return set(self._permissions.keys())

    def has(self, permission_key: str) -> bool:
        return permission_key in self._permissions

    def clear(self):
        """테스트용 초기화."""
        self._permissions.clear()


# 전역 싱글톤
registry = _PermissionRegistry()


# ─────────────────────────────────────────────────────────
# 데코레이터 (FastAPI Dependency)
# ─────────────────────────────────────────────────────────

def require_perm(*permissions: str, description: str = ""):
    """FastAPI Depends 팩토리: 권한 체크 + 레지스트리 자동 등록.

    엔드포인트에 이 데코레이터를 적용하면:
      - import 시점에 permission이 레지스트리에 등록됨
      - 요청 시점에 Gateway 헤더 기반 권한 체크
      - superuser는 자동 통과

    Usage:
        @router.get("/roles")
        async def get_roles(request: Request,
                            session=Depends(require_perm("admin.role:read",
                                                         description="역할 조회"))):
            user_id = session["user_id"]
            ...

    Multiple (AND logic):
        session=Depends(require_perm("admin.user:delete", "admin.user:read",
                                     description="사용자 삭제"))
    """
    # import 시점에 레지스트리 등록 (서버 시작 시 자동 수집)
    for perm in permissions:
        registry.register(perm, description)

    async def _dependency(request: Request):
        from xgen_sdk.auth.gateway import get_user_info_by_gateway
        from xgen_sdk.auth.permission_resolver import has_permission

        user_session = get_user_info_by_gateway(request)
        user_perms = user_session.get("permissions", set())

        # superuser bypass
        if user_session.get("is_superuser"):
            return user_session

        for perm in permissions:
            if not has_permission(user_perms, perm):
                raise HTTPException(status_code=403, detail=f"Permission denied: {perm}")

        return user_session

    return _dependency


def require_any_perm(*permissions: str, description: str = ""):
    """FastAPI Depends 팩토리: OR 로직 권한 체크 + 레지스트리 자동 등록.

    주어진 권한 중 **하나라도** 충족하면 통과.

    Usage:
        session=Depends(require_any_perm("admin.user:read", "admin.role:read",
                                         description="관리 대시보드"))
    """
    for perm in permissions:
        registry.register(perm, description)

    async def _dependency(request: Request):
        from xgen_sdk.auth.gateway import get_user_info_by_gateway
        from xgen_sdk.auth.permission_resolver import has_permission

        user_session = get_user_info_by_gateway(request)
        user_perms = user_session.get("permissions", set())

        if user_session.get("is_superuser"):
            return user_session

        if not any(has_permission(user_perms, perm) for perm in permissions):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: one of {list(permissions)} required"
            )

        return user_session

    return _dependency


# ─────────────────────────────────────────────────────────
# 시작 시 DB 동기화
# ─────────────────────────────────────────────────────────

def validate_and_sync(app_db, permission_model_class=None) -> dict:
    """서버 시작 시 호출: 데코레이터로 수집된 권한을 DB와 동기화.

    1) 레지스트리(엔드포인트 데코레이터에서 수집)의 권한 → DB에 누락분 INSERT
    2) DB에 description이 비어있으면 레지스트리 값으로 업데이트

    주의: 다중 서비스(xgen-core, xgen-workflow, xgen-documents)가 각각 독립적으로
    validate_and_sync() 를 호출하므로, orphan 감지는 수행하지 않는다.
    (각 서비스의 레지스트리는 자신의 권한만 포함하므로 다른 서비스의 권한을
     orphan으로 오판하는 문제가 있다.)

    Args:
        app_db: XgenDB 인스턴스
        permission_model_class: Permission ORM 모델 클래스 (생략 시 SDK 내장 모델 사용)

    Returns:
        {"registered": int, "synced": int, "warnings": list}
    """
    Permission = permission_model_class or _PermissionModel

    result = {
        "registered": len(registry.all_permissions()),
        "synced": 0,
        "warnings": [],
    }

    # DB에서 기존 권한 조회
    existing_db = app_db.find_all(Permission, limit=10000)
    existing_map: Dict[str, object] = {}
    if existing_db:
        for p in existing_db:
            existing_map[f"{p.resource}:{p.action}"] = p

    # 레지스트리 → DB 동기화
    for key, info in registry.all_permissions().items():
        if key not in existing_map:
            # 새 권한 INSERT
            perm = Permission(
                resource=info["resource"],
                action=info["action"],
                description=info.get("description", ""),
            )
            try:
                app_db.insert(perm)
                result["synced"] += 1
                logger.info(f"Permission synced to DB: {key}")
            except Exception as e:
                result["warnings"].append(f"Insert error for {key}: {e}")
        else:
            # description 업데이트 (DB가 비어있고 레지스트리에 있으면)
            db_perm = existing_map[key]
            new_desc = info.get("description", "")
            if new_desc and not getattr(db_perm, "description", ""):
                db_perm.description = new_desc
                try:
                    app_db.update(db_perm)
                except Exception:
                    pass

    logger.info(
        f"Permission sync complete: {result['registered']} registered, "
        f"{result['synced']} synced"
    )
    return result
