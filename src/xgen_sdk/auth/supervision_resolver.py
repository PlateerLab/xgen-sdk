"""
Supervision Resolver

역할 간 감독(Supervision) 관계를 해석하는 로직.

감독 유형:
    - full: 전체 감독 (사용자 조회/관리, 역할 변경, 비활성화, 권한 수정)
    - monitor: 모니터링만 (사용자 조회, 활동 로그 읽기)
    - audit: 감사만 (활동 이력, 권한 변경 로그 조회)

Supervision은 Permission과 독립적인 별도 레이어.
    - Permission = 어떤 작업(action)을 수행할 수 있는가
    - Supervision = 어떤 사용자/역할을 감독할 수 있는가
"""
import logging
from typing import Dict, List, Set

logger = logging.getLogger("supervision-resolver")


def resolve_supervision_scope(
    app_db,
    user_id: int,
    is_superuser: bool
) -> Dict[str, List[str]]:
    """사용자의 감독 범위를 역할 이름 목록으로 해석한다.

    Args:
        app_db: AppDatabaseManager 인스턴스
        user_id: 사용자 ID
        is_superuser: superuser 여부

    Returns:
        Dict[str, List[str]]: 감독 유형별 대상 역할 이름 목록
        예: {
            "full": ["developer", "designer"],
            "monitor": ["operator"],
            "audit": ["viewer"]
        }
    """
    if is_superuser:
        # superuser는 모든 역할을 full 감독
        all_roles = _get_all_role_names(app_db)
        return {
            "full": all_roles,
            "monitor": [],
            "audit": []
        }

    query = """
        SELECT DISTINCT
            rs.supervision_type,
            r_target.name AS target_role_name
        FROM role_supervision rs
        JOIN roles r_target ON r_target.id = rs.target_role_id
        JOIN user_roles ur ON ur.role_id = rs.supervisor_role_id
        WHERE ur.user_id = %s
        ORDER BY rs.supervision_type, r_target.name
    """
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        scope = {"full": [], "monitor": [], "audit": []}

        if result.get("success") and result.get("data"):
            for row in result["data"]:
                sup_type = row["supervision_type"]
                role_name = row["target_role_name"]
                if sup_type in scope:
                    scope[sup_type].append(role_name)

        return scope
    except Exception as e:
        logger.error(f"Failed to resolve supervision scope for user {user_id}: {e}")
        return {"full": [], "monitor": [], "audit": []}


def resolve_supervised_user_ids(
    app_db,
    user_id: int,
    is_superuser: bool
) -> Dict[str, Set[int]]:
    """사용자의 감독 범위를 대상 사용자 ID 집합으로 해석한다.

    Args:
        app_db: AppDatabaseManager 인스턴스
        user_id: 사용자 ID
        is_superuser: superuser 여부

    Returns:
        Dict[str, Set[int]]: 감독 유형별 대상 사용자 ID 집합
        예: {
            "full": {1, 2, 5},
            "monitor": {3},
            "audit": {4, 6}
        }
    """
    if is_superuser:
        # superuser는 모든 사용자를 full 감독
        all_user_ids = _get_all_user_ids(app_db)
        return {
            "full": all_user_ids,
            "monitor": set(),
            "audit": set()
        }

    query = """
        SELECT DISTINCT
            rs.supervision_type,
            target_ur.user_id AS target_user_id
        FROM role_supervision rs
        JOIN user_roles ur ON ur.role_id = rs.supervisor_role_id
        JOIN user_roles target_ur ON target_ur.role_id = rs.target_role_id
        WHERE ur.user_id = %s
        ORDER BY rs.supervision_type
    """
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        scope = {"full": set(), "monitor": set(), "audit": set()}

        if result.get("success") and result.get("data"):
            for row in result["data"]:
                sup_type = row["supervision_type"]
                target_uid = row["target_user_id"]
                if sup_type in scope:
                    scope[sup_type].add(target_uid)

        return scope
    except Exception as e:
        logger.error(f"Failed to resolve supervised users for user {user_id}: {e}")
        return {"full": set(), "monitor": set(), "audit": set()}


def can_supervise_user(
    app_db,
    supervisor_user_id: int,
    target_user_id: int,
    is_superuser: bool,
    required_level: str = "monitor"
) -> bool:
    """특정 사용자를 감독할 수 있는지 확인한다.

    감독 수준 포함 관계: full > monitor > audit
        - full 권한이 있으면 monitor, audit 도 가능
        - monitor 권한이 있으면 audit 도 가능

    Args:
        app_db: AppDatabaseManager 인스턴스
        supervisor_user_id: 감독자 사용자 ID
        target_user_id: 대상 사용자 ID
        is_superuser: 감독자의 superuser 여부
        required_level: 필요한 감독 수준 ("full" | "monitor" | "audit")

    Returns:
        bool: 감독 가능 여부
    """
    if is_superuser:
        return True

    # 감독 수준별 허용 유형 (포함 관계)
    allowed_types = _get_allowed_supervision_types(required_level)

    scope = resolve_supervised_user_ids(app_db, supervisor_user_id, False)

    for sup_type in allowed_types:
        if target_user_id in scope.get(sup_type, set()):
            return True

    return False


# ─────────────────────────────────────────────────────────
# 내부 헬퍼 함수
# ─────────────────────────────────────────────────────────

def _get_allowed_supervision_types(required_level: str) -> List[str]:
    """감독 수준에 따라 허용되는 유형 목록 반환.

    full > monitor > audit (포함 관계)
    """
    hierarchy = {
        "full": ["full"],
        "monitor": ["full", "monitor"],
        "audit": ["full", "monitor", "audit"]
    }
    return hierarchy.get(required_level, ["full", "monitor", "audit"])


def _get_all_role_names(app_db) -> List[str]:
    """모든 역할 이름을 조회한다."""
    query = "SELECT name FROM roles ORDER BY name"
    try:
        result = app_db.execute_raw_query(query)
        if result.get("success") and result.get("data"):
            return [row["name"] for row in result["data"]]
        return []
    except Exception as e:
        logger.error(f"Failed to get all role names: {e}")
        return []


def _get_all_user_ids(app_db) -> Set[int]:
    """모든 사용자 ID를 조회한다."""
    query = "SELECT id FROM users"
    try:
        result = app_db.execute_raw_query(query)
        if result.get("success") and result.get("data"):
            return {row["id"] for row in result["data"]}
        return set()
    except Exception as e:
        logger.error(f"Failed to get all user ids: {e}")
        return set()
