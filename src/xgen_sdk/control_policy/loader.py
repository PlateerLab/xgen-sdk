"""통제 정책 읽기: 활성 정책 + 에이전트별 예외.

``db`` 는 ``find_records_by_condition(table, conditions)`` 를 가진 앱 DB(:class:`xgen_sdk.db.AppDatabaseManager`)다.
읽지 못하면 빈 목록: 정책 조회 장애가 대화 · 업로드를 막지 않는다(예전 적용기와 같은 open-by-default).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, List, Optional, Sequence

from .errors import PolicyError, PolicyInvalidError, PolicyUnavailableError
from .outcome import PolicyLoadResult, PolicyLoadState
from .rules import MaskingRule, compile_rules, compile_rules_strict
from .settings import FlagReader, PolicyKind, kinds_on, kinds_on_strict

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
        logger.debug("정책 예외 조회 실패: code=policy_exceptions_unavailable")
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
        logger.warning("통제 정책 조회 실패: code=policy_store_unavailable")
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


def excepted_policy_ids_strict(db: Any, workflow_id: Optional[str], policy_type: str, *,
                               request_id: Optional[str] = None) -> set:
    """Strict exception lookup; a failed/malformed read never means no exceptions."""
    if not workflow_id:
        return set()
    if db is None or not hasattr(db, "find_records_by_condition"):
        raise PolicyUnavailableError("policy_store_unavailable", request_id=request_id)
    try:
        rows = db.find_records_by_condition(
            EXCEPTIONS_TABLE, {"workflow_id": workflow_id, "policy_type": policy_type}, limit=_ROW_LIMIT,
        )
    except Exception:  # noqa: BLE001
        raise PolicyUnavailableError("policy_exceptions_unavailable", request_id=request_id) from None
    if rows is None:
        raise PolicyUnavailableError("policy_exceptions_unavailable", request_id=request_id)
    if not isinstance(rows, (list, tuple)):
        raise PolicyInvalidError("policy_exceptions_invalid", request_id=request_id)
    excluded = set()
    for row in rows:
        if not isinstance(row, Mapping) or row.get("policy_id") is None or not isinstance(row.get("is_applied"), bool):
            raise PolicyInvalidError("policy_exceptions_invalid", request_id=request_id)
        if row["is_applied"] is False:
            excluded.add(row["policy_id"])
    return excluded


def _revision_from_rows(rows: Sequence[Mapping[str, Any]]) -> Optional[str]:
    revisions = []
    for row in rows:
        value = next((row.get(key) for key in ("revision", "version", "updated_at") if row.get(key) is not None), None)
        if value is not None:
            revisions.append(f"{row.get('id')}:{value}")
    return "|".join(revisions) if revisions else None


def _failure(exc: PolicyError, scope: Optional[str]) -> PolicyLoadResult:
    state = PolicyLoadState.INVALID if isinstance(exc, PolicyInvalidError) else PolicyLoadState.UNAVAILABLE
    return PolicyLoadResult(
        state=state,
        scope=scope,
        error_code=exc.code,
        request_id=exc.request_id,
        retryable=exc.retryable,
    )


def load_rules_strict(db: Any, kind: PolicyKind, *, workflow_id: Optional[str] = None,
                      unmasked: str = "skip", scope: Optional[str] = None,
                      revision: Optional[str] = None,
                      request_id: Optional[str] = None) -> PolicyLoadResult:
    """Load one policy kind while preserving empty, unavailable and invalid states."""
    try:
        if not isinstance(kind, PolicyKind):
            raise PolicyInvalidError("policy_kind_invalid", request_id=request_id)
        if db is None or not hasattr(db, "find_records_by_condition"):
            raise PolicyUnavailableError("policy_store_unavailable", request_id=request_id)
        try:
            rows = db.find_records_by_condition(kind.table, {"is_active": True}, limit=_ROW_LIMIT)
        except Exception:  # noqa: BLE001
            raise PolicyUnavailableError("policy_store_unavailable", request_id=request_id) from None
        if rows is None:
            raise PolicyUnavailableError("policy_store_unavailable", request_id=request_id)
        if not isinstance(rows, (list, tuple)):
            raise PolicyInvalidError("policy_data_invalid", request_id=request_id)
        excluded = excepted_policy_ids_strict(
            db, workflow_id, kind.policy_type, request_id=request_id,
        )
        compiled = compile_rules_strict(
            rows, kind.policy_type, unmasked=unmasked, request_id=request_id,
        )
        rules = [rule for rule in compiled if rule.policy_id not in excluded]
        return PolicyLoadResult(
            state=PolicyLoadState.LOADED,
            rules=tuple(rules),
            revision=revision if revision is not None else _revision_from_rows(rows),
            scope=scope,
        )
    except PolicyError as exc:
        return _failure(exc, scope)


def load_scope_rules_strict(db: Any, scope: str, *, workflow_id: Optional[str] = None,
                            flag: Optional[FlagReader] = None, config: Any = None,
                            request_id: Optional[str] = None) -> PolicyLoadResult:
    """Load a scope with explicit disabled/loaded/failure states."""
    try:
        enabled = kinds_on_strict(
            scope, flag=flag, config=config, request_id=request_id,
        )
    except PolicyError as exc:
        return _failure(exc, scope)
    if not enabled:
        return PolicyLoadResult(state=PolicyLoadState.DISABLED, scope=scope)

    rules: List[MaskingRule] = []
    revisions = []
    for kind in enabled:
        result = load_rules_strict(
            db, kind, workflow_id=workflow_id, scope=scope, request_id=request_id,
        )
        if result.is_failure:
            return result
        rules.extend(result.rules)
        if result.revision:
            revisions.append(f"{kind.policy_type}:{result.revision}")
    return PolicyLoadResult(
        state=PolicyLoadState.LOADED,
        rules=tuple(rules),
        revision="|".join(revisions) if revisions else None,
        scope=scope,
    )
