"""
Permission Resolver

사용자의 최종 권한을 해석하는 핵심 로직.

최종 권한 =
    if is_superuser: { "*:*" }
    else: (사용자 역할의 권한 합집합) ∪ (그룹 역할의 권한 합집합) ∪ (직접 추가 권한) - (직접 차단 권한)
"""
import logging
from typing import Set, List, Optional

logger = logging.getLogger("permission-resolver")


def resolve_user_permissions(
    app_db,
    user_id: int,
    is_superuser: bool,
    groups: Optional[List[str]] = None
) -> Set[str]:
    """사용자의 최종 권한 집합을 해석한다.

    Args:
        app_db: AppDatabaseManager 인스턴스
        user_id: 사용자 ID
        is_superuser: superuser 여부
        groups: 사용자가 소속된 그룹 이름 목록

    Returns:
        Set[str]: permission 문자열 집합 (예: {"workflow:create", "document:read"})
    """
    if is_superuser:
        return {"*:*"}

    # 1. 사용자 역할의 권한 (user_roles → role_permissions → permissions)
    role_perms = _get_permissions_from_user_roles(app_db, user_id)

    # 2. 그룹 역할의 권한 (groups → group_meta → group_roles → role_permissions → permissions)
    group_perms = set()
    if groups:
        group_perms = _get_permissions_from_group_roles(app_db, groups)

    # 3. 직접 부여 (user_permissions WHERE granted=TRUE)
    granted = _get_directly_granted_permissions(app_db, user_id)

    # 4. 직접 차단 (user_permissions WHERE granted=FALSE)
    denied = _get_directly_denied_permissions(app_db, user_id)

    # 5. 합산: (역할 권한 ∪ 그룹 권한 ∪ 직접 부여) - 직접 차단
    final = (role_perms | group_perms | granted) - denied

    logger.debug(
        f"Resolved permissions for user {user_id}: "
        f"role={len(role_perms)}, group={len(group_perms)}, "
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
    query = """
        SELECT r.name
        FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = %s
    """
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        if result.get("success") and result.get("data"):
            return [row['name'] for row in result["data"]]
        return []
    except Exception as e:
        logger.error(f"Failed to get user roles for user {user_id}: {e}")
        return []


# ─────────────────────────────────────────────────────────
# 내부 헬퍼 함수
# ─────────────────────────────────────────────────────────

def _get_permissions_from_user_roles(app_db, user_id: int) -> Set[str]:
    """user_roles → role_permissions → permissions 경로로 권한 조회"""
    query = """
        SELECT DISTINCT p.resource || ':' || p.action AS perm
        FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        JOIN user_roles ur ON ur.role_id = rp.role_id
        WHERE ur.user_id = %s
    """
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        if result.get("success") and result.get("data"):
            return {row['perm'] for row in result["data"]}
        return set()
    except Exception as e:
        logger.error(f"Failed to get role permissions for user {user_id}: {e}")
        return set()


def _get_permissions_from_group_roles(app_db, groups: List[str]) -> Set[str]:
    """groups → group_meta → group_roles → role_permissions → permissions 경로로 권한 조회"""
    if not groups:
        return set()

    placeholders = ", ".join(["%s"] * len(groups))
    query = f"""
        SELECT DISTINCT p.resource || ':' || p.action AS perm
        FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        JOIN group_roles gr ON gr.role_id = rp.role_id
        JOIN group_meta gm ON gm.id = gr.group_id
        WHERE gm.group_name IN ({placeholders})
          AND gm.available = TRUE
    """
    try:
        result = app_db.execute_raw_query(query, tuple(groups))
        if result.get("success") and result.get("data"):
            return {row['perm'] for row in result["data"]}
        return set()
    except Exception as e:
        logger.error(f"Failed to get group role permissions for groups {groups}: {e}")
        return set()


def _get_directly_granted_permissions(app_db, user_id: int) -> Set[str]:
    """user_permissions WHERE granted=TRUE 경로로 직접 부여 권한 조회"""
    query = """
        SELECT DISTINCT p.resource || ':' || p.action AS perm
        FROM permissions p
        JOIN user_permissions up ON up.permission_id = p.id
        WHERE up.user_id = %s AND up.granted = TRUE
    """
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        if result.get("success") and result.get("data"):
            return {row['perm'] for row in result["data"]}
        return set()
    except Exception as e:
        logger.error(f"Failed to get granted permissions for user {user_id}: {e}")
        return set()


def _get_directly_denied_permissions(app_db, user_id: int) -> Set[str]:
    """user_permissions WHERE granted=FALSE 경로로 직접 차단 권한 조회"""
    query = """
        SELECT DISTINCT p.resource || ':' || p.action AS perm
        FROM permissions p
        JOIN user_permissions up ON up.permission_id = p.id
        WHERE up.user_id = %s AND up.granted = FALSE
    """
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        if result.get("success") and result.get("data"):
            return {row['perm'] for row in result["data"]}
        return set()
    except Exception as e:
        logger.error(f"Failed to get denied permissions for user {user_id}: {e}")
        return set()
