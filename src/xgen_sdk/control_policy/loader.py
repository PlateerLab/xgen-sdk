"""통제 정책 읽기: 활성 정책 + 에이전트별 예외.

``db`` 는 ``find_records_by_condition(table, conditions)`` 를 가진 앱 DB(:class:`xgen_sdk.db.AppDatabaseManager`)다.
읽지 못하면 빈 목록: 정책 조회 장애가 대화 · 업로드를 막지 않는다(예전 적용기와 같은 open-by-default).
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from .rules import MaskingRule, compile_rules
from .settings import FlagReader, PolicyKind, kinds_on

logger = logging.getLogger("xgen-sdk.control-policy")

#: 정책 × 에이전트 예외 표: ``is_applied=False`` 인 줄이 "이 에이전트에는 이 정책을 쓰지 않는다".
EXCEPTIONS_TABLE = "agent_applied_policies"
#: 한 번에 읽는 줄 수. 앱 DB 조회의 기본 상한(500)에 걸려 오래된 정책이 말없이 빠지지 않게 넉넉히.
_ROW_LIMIT = 100_000


def excepted_policy_ids(db: Any, workflow_id: Optional[str], policy_type: str) -> set:
    """이 에이전트에서 예외로 뺀 정책 id. 에이전트가 없거나(파일 저장소 등) 줄이 없으면 빈 집합."""
    if not workflow_id:
        return set()
    try:
        rows = db.find_records_by_condition(
            EXCEPTIONS_TABLE, {"workflow_id": workflow_id, "policy_type": policy_type}, limit=_ROW_LIMIT,
        ) or []
    except Exception:  # noqa: BLE001
        logger.debug("정책 예외 조회 실패 (%s, %s)", workflow_id, policy_type, exc_info=True)
        return set()
    return {row.get("policy_id") for row in rows if row.get("is_applied") is False}


def load_rules(db: Any, kind: PolicyKind, *, workflow_id: Optional[str] = None,
               unmasked: str = "skip") -> List[MaskingRule]:
    """이 종류의 활성 정책 → 규칙. ``workflow_id`` 를 주면 그 에이전트의 예외를 뺀다."""
    if db is None:
        return []
    try:
        rows = db.find_records_by_condition(kind.table, {"is_active": True}, limit=_ROW_LIMIT) or []
    except Exception:  # noqa: BLE001
        logger.warning("통제 정책 조회 실패: %s", kind.table, exc_info=True)
        return []
    excepted = excepted_policy_ids(db, workflow_id, kind.policy_type)
    if excepted:
        rows = [row for row in rows if row.get("id") not in excepted]
    return compile_rules(rows, kind.policy_type, unmasked=unmasked)


def load_scope_rules(db: Any, scope: str, *, workflow_id: Optional[str] = None,
                     flag: Optional[FlagReader] = None, config: Any = None) -> List[MaskingRule]:
    """이 자리에 켜진 종류의 규칙을 적용 차례대로(개인정보 다음 금칙어).

    [마스킹 미적용] 정책은 뺀다: 관리자가 가리지 말라고 한 정책을 막거나 지우지 않는다.
    """
    rules: List[MaskingRule] = []
    for kind in kinds_on(scope, flag=flag, config=config):
        rules.extend(load_rules(db, kind, workflow_id=workflow_id))
    return rules
