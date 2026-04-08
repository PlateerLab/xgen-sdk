"""
Permission Registry — 단일 진실 소스 (Single Source of Truth)

모든 권한은 이 레지스트리를 통해 등록/조회/검증된다.

사용 패턴:
    1) permission_constants.py 에서 ALL_PERMISSIONS 정의 (정적)
    2) 컨트롤러에서 @require_perm("admin.user:read") 데코레이터 사용 → 자동 등록
    3) 서버 시작 시 validate_and_sync() 로 불일치 검출 + DB 동기화

이렇게 하면:
    - 새 기능 추가: 컨트롤러에 @require_perm 데코레이터만 달면 자동 등록 + DB 동기화
    - 기능 제거: 데코레이터 제거 → 시작 시 "orphaned" 경고 로그
    - 이원화 불가: 컨트롤러가 곧 권한 정의
"""
import logging
from typing import Set, Dict, List, Tuple, Optional
from functools import wraps
from fastapi import Request, HTTPException, Depends

logger = logging.getLogger("permission-registry")


# ─────────────────────────────────────────────────────────
# Global Registry
# ─────────────────────────────────────────────────────────

class _PermissionRegistry:
    """싱글톤 권한 레지스트리."""

    def __init__(self):
        # { "admin.user:read": {"description": "...", "sources": {"constant", "decorator"}} }
        self._permissions: Dict[str, Dict] = {}
        self._route_map: Dict[str, List[str]] = {}  # "GET /api/admin/user/all-users" → ["admin.user:read"]

    def register(self, resource: str, action: str, description: str = "", source: str = "constant"):
        """권한을 레지스트리에 등록."""
        key = f"{resource}:{action}"
        if key not in self._permissions:
            self._permissions[key] = {"resource": resource, "action": action, "description": description, "sources": set()}
        self._permissions[key]["sources"].add(source)
        if description and not self._permissions[key]["description"]:
            self._permissions[key]["description"] = description

    def register_route(self, method: str, path: str, permission: str):
        """라우트 → 권한 매핑 기록."""
        route_key = f"{method.upper()} {path}"
        if route_key not in self._route_map:
            self._route_map[route_key] = []
        if permission not in self._route_map[route_key]:
            self._route_map[route_key].append(permission)

    def all_permissions(self) -> Dict[str, Dict]:
        """등록된 전체 권한 사전."""
        return dict(self._permissions)

    def all_permission_keys(self) -> Set[str]:
        """등록된 전체 권한 문자열 집합."""
        return set(self._permissions.keys())

    def all_route_map(self) -> Dict[str, List[str]]:
        """라우트 → 권한 매핑."""
        return dict(self._route_map)

    def has(self, permission_key: str) -> bool:
        return permission_key in self._permissions

    def clear(self):
        """테스트용 초기화."""
        self._permissions.clear()
        self._route_map.clear()


# 전역 싱글톤
registry = _PermissionRegistry()


# ─────────────────────────────────────────────────────────
# 상수에서 자동 등록
# ─────────────────────────────────────────────────────────

def load_constants():
    """permission_constants.py의 ALL_PERMISSIONS를 레지스트리에 등록."""
    from xgen_sdk.auth.permission_constants import ALL_PERMISSIONS
    for resource, action, description in ALL_PERMISSIONS:
        registry.register(resource, action, description, source="constant")
    logger.info(f"Loaded {len(ALL_PERMISSIONS)} permissions from constants")


# ─────────────────────────────────────────────────────────
# 데코레이터 (FastAPI Dependency)
# ─────────────────────────────────────────────────────────

def require_perm(*permissions: str, description: str = ""):
    """FastAPI 라우트 데코레이터: 권한 체크 + 레지스트리 자동 등록.

    Usage:
        @router.get("/all-users")
        async def get_all_users(request: Request, _=Depends(require_perm("admin.user:read"))):
            ...

    Or multiple:
        @router.post("/delete")
        async def delete_user(request: Request, _=Depends(require_perm("admin.user:delete", "admin.user:read"))):
            ...
    """
    # 데코레이터 시점에 레지스트리에 등록 (import 시점)
    for perm in permissions:
        parts = perm.split(":")
        if len(parts) == 2:
            registry.register(parts[0], parts[1], description, source="decorator")

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


# ─────────────────────────────────────────────────────────
# 시작 시 검증 + DB 동기화
# ─────────────────────────────────────────────────────────

def validate_and_sync(app_db, permission_model_class=None) -> Dict:
    """서버 시작 시 호출: 검증 + DB 동기화.

    1) constants와 decorator에서 수집된 권한 비교 → 불일치 경고
    2) DB permissions 테이블과 레지스트리 비교 → 누락분 INSERT
    3) DB에만 있고 레지스트리에 없는 orphan 경고

    Args:
        app_db: XgenDB 인스턴스
        permission_model_class: Permission ORM 모델 클래스 (xgen-core에서 전달)

    Returns:
        {
            "registered": int,
            "inserted": int,
            "orphaned": list,
            "decorator_only": list,
            "warnings": list
        }
    """
    Permission = permission_model_class

    result = {
        "registered": len(registry.all_permissions()),
        "inserted": 0,
        "orphaned": [],
        "decorator_only": [],
        "warnings": []
    }

    # 1) decorator에만 있고 constants에 없는 권한 감지
    for key, info in registry.all_permissions().items():
        sources = info["sources"]
        if "decorator" in sources and "constant" not in sources:
            result["decorator_only"].append(key)
            result["warnings"].append(
                f"⚠️  '{key}' is used in code (@require_perm) but NOT in permission_constants.py"
            )

    # 2) DB 동기화
    existing_db = app_db.find_all(Permission, limit=10000)
    existing_set = set()
    if existing_db:
        for p in existing_db:
            existing_set.add(f"{p.resource}:{p.action}")

    # 레지스트리 → DB 삽입
    for key, info in registry.all_permissions().items():
        if key not in existing_set:
            perm = Permission(
                resource=info["resource"],
                action=info["action"],
                description=info["description"]
            )
            try:
                insert_result = app_db.insert(perm)
                if insert_result is not None:
                    result["inserted"] += 1
                else:
                    result["warnings"].append(f"Failed to insert: {key}")
            except Exception as e:
                result["warnings"].append(f"Insert error for {key}: {e}")

    # 3) DB에 있지만 레지스트리에 없는 orphan 감지
    registry_keys = registry.all_permission_keys()
    for db_key in existing_set:
        if db_key not in registry_keys:
            result["orphaned"].append(db_key)
            result["warnings"].append(
                f"🗑️  '{db_key}' exists in DB but not in code — consider removing"
            )

    return result
