"""
xgen_sdk.auth.object_guard — 객체 단위 소유권/접근 가드 헬퍼.

엔드포인트 레벨에서 `require_perm` 으로 permission 확인 후, 실제 리소스(row)
소유권은 이 모듈의 헬퍼로 확인한다. FastAPI `HTTPException(403/404)` 를 raise 하며
동작은 일관성 있는 에러 응답을 보장한다.

예시:
    from xgen_sdk.auth.object_guard import assert_owns_execution_io

    @router.post("/feedback")
    async def post_feedback(body: FeedbackBody, ...):
        user = await get_user_info_by_gateway(request)
        assert_owns_execution_io(app_db, int(user["user_id"]), body.execution_io_id)
        ...

모든 헬퍼는 "superuser 우회" 를 기본 활성화한다 (호출 시 is_superuser=True 로 스킵).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException


# ──────────────────────────────────────────
# 내부 유틸
# ──────────────────────────────────────────


def _first_row(app_db, sql: str, params: tuple) -> Optional[Dict[str, Any]]:
    res = app_db.execute_raw_query(sql, params)
    if isinstance(res, dict):
        data = res.get("data") or []
        if data:
            return data[0]
    return None


def _normalize_user_id(user_id: Any) -> int:
    try:
        return int(user_id)
    except (TypeError, ValueError) as exc:  # pragma: no cover
        raise HTTPException(status_code=401, detail="Invalid user_id") from exc


# ──────────────────────────────────────────
# 소유권 체크 헬퍼
# ──────────────────────────────────────────


def assert_owns_execution_io(
    app_db,
    user_id: Any,
    execution_io_id: int,
    *,
    is_superuser: bool = False,
) -> Dict[str, Any]:
    """ExecutionIO row 가 해당 user 의 것인지 확인. row dict 반환.

    raises:
        HTTPException(404): row 없음
        HTTPException(403): 소유자 불일치
    """
    row = _first_row(
        app_db,
        "SELECT * FROM execution_io WHERE id = %s LIMIT 1",
        (int(execution_io_id),),
    )
    if not row:
        raise HTTPException(status_code=404, detail="ExecutionIO not found")
    if is_superuser:
        return row
    if int(row.get("user_id", 0)) != _normalize_user_id(user_id):
        raise HTTPException(status_code=403, detail="본인 채팅의 실행 기록이 아닙니다")
    return row


def assert_owns_feedback(
    app_db,
    user_id: Any,
    feedback_id: int,
    *,
    is_superuser: bool = False,
) -> Dict[str, Any]:
    """UserFeedback row 가 해당 user 의 것인지 확인."""
    row = _first_row(
        app_db,
        "SELECT * FROM user_feedback WHERE id = %s LIMIT 1",
        (int(feedback_id),),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Feedback not found")
    if is_superuser:
        return row
    if int(row.get("written_by", 0)) != _normalize_user_id(user_id):
        raise HTTPException(status_code=403, detail="본인의 피드백만 접근할 수 있습니다")
    return row


def assert_owns_test_run(
    app_db,
    user_id: Any,
    test_run_id: int,
    *,
    is_superuser: bool = False,
) -> Dict[str, Any]:
    """TestRun row 가 해당 user 의 것인지 확인."""
    row = _first_row(
        app_db,
        "SELECT * FROM test_runs WHERE id = %s LIMIT 1",
        (int(test_run_id),),
    )
    if not row:
        raise HTTPException(status_code=404, detail="TestRun not found")
    if is_superuser:
        return row
    if int(row.get("triggered_by", 0)) != _normalize_user_id(user_id):
        raise HTTPException(status_code=403, detail="본인의 TestRun 만 접근할 수 있습니다")
    return row


def assert_owns_workflow(
    app_db,
    user_id: Any,
    workflow_id: str,
    *,
    is_superuser: bool = False,
    allow_shared: bool = False,
) -> Dict[str, Any]:
    """WorkflowMeta 의 소유권 확인.

    allow_shared=True 일 때는 `is_shared = TRUE` 인 워크플로우에 대해 공유 사용자도 통과.
    """
    row = _first_row(
        app_db,
        "SELECT * FROM workflow_meta WHERE workflow_id = %s LIMIT 1",
        (str(workflow_id),),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if is_superuser:
        return row
    if int(row.get("user_id", 0)) == _normalize_user_id(user_id):
        return row
    if allow_shared and bool(row.get("is_shared")):
        return row
    raise HTTPException(status_code=403, detail="해당 워크플로우 접근 권한이 없습니다")


__all__ = [
    "assert_owns_execution_io",
    "assert_owns_feedback",
    "assert_owns_test_run",
    "assert_owns_workflow",
]
