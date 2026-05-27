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

    2026-05-27 — 무소속 사용자 합집합 추가:
        "무소속" 정의는 `user_roles` 테이블에 row 가 하나도 없는 사용자
        (admin 권한 보유 여부 무관). 단 `users.is_superuser=True` 인 사용자는
        제외 — superuser 데이터는 다른 관리자에게 보이지 않아야 함.

        무소속 사용자는 어떤 조직/역할에도 속하지 않으므로 모든 관리자(superuser
        가 아닌 supervision 권한자) 의 관리 대상으로 본다. 호출자는 이미
        is_superuser=False 진입 경로 (admin*Controller 들이 superuser 분기 후
        호출) 이므로 본 합집합 추가는 안전.

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
    supervised: Set[int] = set()
    try:
        result = app_db.execute_raw_query(query, (user_id,))
        if result.get("success") and result.get("data"):
            supervised = {row["target_user_id"] for row in result["data"]}
    except Exception as e:
        logger.error(f"Failed to resolve supervised users for user {user_id}: {e}")
        # 기존 동작 호환 — 감독 관계 조회 실패 시 본 함수는 빈 set 반환했음.
        # 무소속 합집합은 별도 try/except 로 격리하여 한쪽 실패가 다른 쪽 동작을
        # 깨지 않도록 한다.

    # 무소속(role 없음 + superuser 아님) 사용자 합집합.
    # 실패해도 graceful — supervised set 만 반환 (기존 동작과 동일).
    supervised |= _get_unaffiliated_user_ids(app_db)

    return supervised


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


def _get_unaffiliated_user_ids(app_db) -> Set[int]:
    """무소속(role 보유 0건) + superuser 아닌 사용자 ID 집합을 조회한다.

    "무소속" 정의:
        - `user_roles` 테이블에 해당 user_id 의 row 가 한 건도 없는 사용자.
        - admin 권한이 별도 부여되어 있더라도 role 자체가 없으면 무소속.

    superuser 제외:
        - `users.is_superuser=True` 인 사용자는 제외. superuser 데이터가
          다른 관리자에게 보이면 안 되므로 명시적 제외.
        - 컬럼이 NULL 인 옛 row 도 NOT TRUE 평가에서 빠지지 않도록
          `COALESCE(..., FALSE)` 사용.

    실패 시 graceful: 조회 실패해도 호출자 흐름 유지 위해 빈 set 반환.
    """
    query = """
        SELECT u.id
        FROM users u
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        WHERE ur.user_id IS NULL
          AND COALESCE(u.is_superuser, FALSE) = FALSE
    """
    try:
        result = app_db.execute_raw_query(query)
        if result.get("success") and result.get("data"):
            return {row["id"] for row in result["data"]}
        return set()
    except Exception as e:
        logger.warning(
            f"Failed to get unaffiliated user ids — supervision 합집합에서 누락됨 (graceful skip): {e}"
        )
        return set()
