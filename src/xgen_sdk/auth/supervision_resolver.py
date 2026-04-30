"""
Supervision Resolver

역할 간 감독(Supervision) 관계를 해석하는 로직.

2026-04-30 단순화 (Option A):
    이전 버전은 full / monitor / audit 3단계 유형을 노출했으나, 코드베이스 전수 조사 결과
    실질적 차별 처리는 'full'(사용자 편집 + 워크플로우 가시성) 단 하나의 슬롯에서만 발생했고
    monitor / audit 헤더와 dict slot 은 어떤 비즈니스 로직에서도 소비되지 않는 dead infrastructure
    였다. 현실에 정합하도록 supervision 을 단일 'full' 권한으로 통일.

    호환을 위해 DB 의 supervision_type 컬럼은 그대로 유지하되, 신규 row 는 항상 'full' 로 저장
    하며, 조회 시점에는 type 을 무시하고 모든 매핑을 동등하게 취급한다.

    포함관계 / required_level 비교 로직(can_supervise_user)은 호출자가 0 이었으므로 함수 자체를 제거.

Permission 과 Supervision 구분은 그대로:
    - Permission = 어떤 작업(action)을 수행할 수 있는가
    - Supervision = 어떤 사용자/역할을 감독할 수 있는가
"""
import logging
from typing import List, Set

logger = logging.getLogger("supervision-resolver")


def resolve_supervision_scope(
    app_db,
    user_id: int,
    is_superuser: bool
) -> List[str]:
    """사용자가 감독하는 대상 역할 이름 목록을 반환한다.

    이전 버전은 Dict[str, List[str]] (유형별) 을 반환했으나, 모든 호출자는 결과를 합쳐
    한 덩어리로 사용했으므로 단순 List 로 평탄화.

    Args:
        app_db: AppDatabaseManager 인스턴스
        user_id: 사용자 ID
        is_superuser: superuser 여부

    Returns:
        List[str]: 감독 대상 역할 이름 목록 (정렬됨, 중복 제거)
    """
    if is_superuser:
        return _get_all_role_names(app_db)

    query = """
        SELECT DISTINCT r_target.name AS target_role_name
        FROM role_supervision rs
        JOIN roles r_target ON r_target.id = rs.target_role_id
        JOIN user_roles ur ON ur.role_id = rs.supervisor_role_id
        WHERE ur.user_id = %s
        ORDER BY r_target.name
    """
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        if result.get("success") and result.get("data"):
            return [row["target_role_name"] for row in result["data"]]
        return []
    except Exception as e:
        logger.error(f"Failed to resolve supervision scope for user {user_id}: {e}")
        return []


def resolve_supervised_user_ids(
    app_db,
    user_id: int,
    is_superuser: bool
) -> Set[int]:
    """사용자가 감독하는 대상 사용자 ID 집합을 반환한다.

    이전 버전은 Dict[str, Set[int]] (유형별) 을 반환했으나, full 슬롯만 의미 있게
    사용되고 나머지는 합집합으로만 사용되었으므로 단순 Set 로 평탄화.

    Args:
        app_db: AppDatabaseManager 인스턴스
        user_id: 사용자 ID
        is_superuser: superuser 여부

    Returns:
        Set[int]: 감독 가능한 사용자 ID 집합
    """
    if is_superuser:
        return _get_all_user_ids(app_db)

    query = """
        SELECT DISTINCT target_ur.user_id AS target_user_id
        FROM role_supervision rs
        JOIN user_roles ur ON ur.role_id = rs.supervisor_role_id
        JOIN user_roles target_ur ON target_ur.role_id = rs.target_role_id
        WHERE ur.user_id = %s
    """
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        if result.get("success") and result.get("data"):
            return {row["target_user_id"] for row in result["data"]}
        return set()
    except Exception as e:
        logger.error(f"Failed to resolve supervised users for user {user_id}: {e}")
        return set()


# ─────────────────────────────────────────────────────────
# 내부 헬퍼 함수
# ─────────────────────────────────────────────────────────

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
