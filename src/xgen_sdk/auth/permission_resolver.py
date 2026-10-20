"""
Permission Resolver

사용자의 최종 권한을 해석하는 핵심 로직.

최종 권한 =
    if is_superuser: { "*:*" }
    else: (사용자 역할의 권한 합집합) ∪ (직접 추가 권한) - (직접 차단 권한)
"""
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Set, List, Optional

logger = logging.getLogger("permission-resolver")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class PermissionResolutionError(RuntimeError):
    """A redacted authority-store failure safe to map to HTTP 503."""

    def __init__(self, code: str = "permission_store_unavailable", *,
                 request_id: Optional[str] = None, retryable: bool = True) -> None:
        self.code = code if code in {"permission_store_unavailable", "permission_store_invalid"} else "permission_store_unavailable"
        self.request_id = (
            str(request_id) if request_id is not None and _SAFE_REQUEST_ID.fullmatch(str(request_id))
            else uuid.uuid4().hex
        )
        self.retryable = bool(retryable)
        super().__init__(f"{self.code} (request_id={self.request_id})")


def resolve_user_permissions(
    app_db,
    user_id: int,
    is_superuser: bool,
    *,
    request_id: Optional[str] = None,
) -> Set[str]:
    """사용자의 최종 권한 집합을 해석한다.

    Args:
        app_db: AppDatabaseManager 인스턴스
        user_id: 사용자 ID
        is_superuser: superuser 여부

    Returns:
        Set[str]: permission 문자열 집합 (예: {"workflow:create", "document:read"})
    """
    if is_superuser:
        return {"*:*"}

    # 역할·직접 부여·직접 차단을 한 statement 의 DB snapshot 에서 읽는다.
    role_perms, granted, denied = _get_permission_authority_snapshot(
        app_db, user_id, request_id=request_id,
    )
    final = (role_perms | granted) - denied

    logger.debug(
        f"Resolved permissions: role={len(role_perms)}, "
        f"granted={len(granted)}, denied={len(denied)}, "
        f"final={len(final)}"
    )

    return final


def has_permission(user_permissions: set, required: str) -> bool:
    """사용자 권한 집합에서 요구 권한을 와일드카드 매칭 포함하여 체크.

    매칭 우선순위:
        1. 전체 와일드카드: "*:*"
        2. 정확 매치: "workflow:create"
        3. 리소스 와일드카드: "workflow:*" → "workflow:create"
        4. 상위 리소스 매치: "admin.*:*" → "admin.user:create"

    Args:
        user_permissions: 사용자의 permission 문자열 집합
        required: 검증할 permission 문자열

    Returns:
        bool: 권한 있으면 True
    """
    # 1. 전체 와일드카드
    if "*:*" in user_permissions:
        return True

    # 2. 정확 매치
    if required in user_permissions:
        return True

    # 3. 리소스 와일드카드
    parts = required.split(":")
    if len(parts) == 2:
        resource = parts[0]
        if f"{resource}:*" in user_permissions:
            return True

        # 4. 상위 리소스 매치 ("admin.*:*" → "admin.user:create")
        resource_parts = resource.split(".")
        for i in range(1, len(resource_parts)):
            parent = ".".join(resource_parts[:i])
            if f"{parent}.*:*" in user_permissions:
                return True

    return False


def get_user_roles(app_db, user_id: int) -> List[str]:
    """사용자에게 할당된 역할 이름 목록을 조회한다.

    Args:
        app_db: AppDatabaseManager 인스턴스
        user_id: 사용자 ID

    Returns:
        List[str]: 역할 이름 목록
    """
    try:
        return get_user_roles_strict(app_db, user_id)
    except PermissionResolutionError as exc:
        logger.warning("User role lookup failed: code=%s request_id=%s", exc.code, exc.request_id)
        return []


def get_user_roles_strict(app_db, user_id: int, *, request_id: Optional[str] = None) -> List[str]:
    """Return roles only after a successful, well-formed authority-store read."""
    query = """
        SELECT r.name
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = %s
    """
    rows = _read_query_rows(app_db, query, user_id, request_id=request_id)
    roles = []
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise PermissionResolutionError(
                "permission_store_invalid", request_id=request_id, retryable=False,
            )
        roles.append(name)
    return roles


# ─────────────────────────────────────────────────────────
# 내부 헬퍼 함수
# ─────────────────────────────────────────────────────────

def _read_query_rows(app_db, query: str, user_id: int, *,
                     request_id: Optional[str] = None) -> List[Mapping]:
    if app_db is None or not hasattr(app_db, "execute_raw_query"):
        raise PermissionResolutionError(request_id=request_id)
    try:
        result = app_db.execute_raw_query(query, (user_id,))
    except Exception:  # noqa: BLE001
        failure = PermissionResolutionError(request_id=request_id)
        logger.warning("Permission lookup failed: code=%s request_id=%s", failure.code, failure.request_id)
        raise failure from None
    if not isinstance(result, Mapping) or result.get("success") is not True:
        failure = PermissionResolutionError(request_id=request_id)
        logger.warning("Permission lookup failed: code=%s request_id=%s", failure.code, failure.request_id)
        raise failure
    rows = result.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        failure = PermissionResolutionError(
            "permission_store_invalid", request_id=request_id, retryable=False,
        )
        logger.warning("Permission lookup failed: code=%s request_id=%s", failure.code, failure.request_id)
        raise failure
    return rows


def _permission_set(app_db, query: str, user_id: int, *,
                    request_id: Optional[str] = None) -> Set[str]:
    rows = _read_query_rows(app_db, query, user_id, request_id=request_id)
    permissions = set()
    for row in rows:
        permission = row.get("perm")
        if not isinstance(permission, str) or not permission or ":" not in permission:
            raise PermissionResolutionError(
                "permission_store_invalid", request_id=request_id, retryable=False,
            )
        permissions.add(permission)
    return permissions


def _get_permission_authority_snapshot(app_db, user_id: int, *,
                                       request_id: Optional[str] = None) -> tuple[Set[str], Set[str], Set[str]]:
    """Read role grants and direct allow/deny rows in one database statement."""
    query = """
        WITH authority AS (
            SELECT DISTINCT p.resource || ':' || p.action AS perm, 'role' AS source
            FROM permissions p
            JOIN role_permissions rp ON rp.permission_id = p.id
            JOIN user_roles ur ON ur.role_id = rp.role_id
            WHERE ur.user_id = %s
            UNION ALL
            SELECT DISTINCT p.resource || ':' || p.action AS perm,
                   CASE WHEN up.granted = TRUE THEN 'grant' ELSE 'deny' END AS source
            FROM permissions p
            JOIN user_permissions up ON up.permission_id = p.id
            WHERE up.user_id = %s
        )
        SELECT perm, source FROM authority
    """
    if app_db is None or not hasattr(app_db, "execute_raw_query"):
        raise PermissionResolutionError(request_id=request_id)
    try:
        result = app_db.execute_raw_query(query, (user_id, user_id))
    except Exception:  # noqa: BLE001
        failure = PermissionResolutionError(request_id=request_id)
        logger.warning("Permission lookup failed: code=%s request_id=%s", failure.code, failure.request_id)
        raise failure from None
    if not isinstance(result, Mapping) or result.get("success") is not True:
        failure = PermissionResolutionError(request_id=request_id)
        logger.warning("Permission lookup failed: code=%s request_id=%s", failure.code, failure.request_id)
        raise failure
    rows = result.get("data")
    if not isinstance(rows, list):
        failure = PermissionResolutionError(
            "permission_store_invalid", request_id=request_id, retryable=False,
        )
        logger.warning("Permission lookup failed: code=%s request_id=%s", failure.code, failure.request_id)
        raise failure

    by_source = {"role": set(), "grant": set(), "deny": set()}
    for row in rows:
        if not isinstance(row, Mapping):
            raise PermissionResolutionError(
                "permission_store_invalid", request_id=request_id, retryable=False,
            )
        permission = row.get("perm")
        source = row.get("source")
        if (
            not isinstance(permission, str)
            or not permission
            or ":" not in permission
            or source not in by_source
        ):
            raise PermissionResolutionError(
                "permission_store_invalid", request_id=request_id, retryable=False,
            )
        by_source[source].add(permission)
    return by_source["role"], by_source["grant"], by_source["deny"]


def _get_permissions_from_user_roles(app_db, user_id: int, *,
                                     request_id: Optional[str] = None) -> Set[str]:
    """user_roles → role_permissions → permissions 경로로 권한 조회"""
    query = """
        SELECT DISTINCT p.resource || ':' || p.action AS perm
        FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        JOIN user_roles ur ON ur.role_id = rp.role_id
        WHERE ur.user_id = %s
    """
    return _permission_set(app_db, query, user_id, request_id=request_id)


def _get_directly_granted_permissions(app_db, user_id: int, *,
                                      request_id: Optional[str] = None) -> Set[str]:
    """user_permissions WHERE granted=TRUE 경로로 직접 부여 권한 조회"""
    query = """
        SELECT DISTINCT p.resource || ':' || p.action AS perm
        FROM permissions p
        JOIN user_permissions up ON up.permission_id = p.id
        WHERE up.user_id = %s AND up.granted = TRUE
    """
    return _permission_set(app_db, query, user_id, request_id=request_id)


def _get_directly_denied_permissions(app_db, user_id: int, *,
                                     request_id: Optional[str] = None) -> Set[str]:
    """user_permissions WHERE granted=FALSE 경로로 직접 차단 권한 조회"""
    query = """
        SELECT DISTINCT p.resource || ':' || p.action AS perm
        FROM permissions p
        JOIN user_permissions up ON up.permission_id = p.id
        WHERE up.user_id = %s AND up.granted = FALSE
    """
    return _permission_set(app_db, query, user_id, request_id=request_id)


__all__ = [
    "PermissionResolutionError",
    "resolve_user_permissions",
    "has_permission",
    "get_user_roles",
    "get_user_roles_strict",
]
