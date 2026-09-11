"""결재가 승인됐을 때 **무엇을 할지** 를 꽂는 자리.

목록과 동작을 나눈다
--------------------
  * **무엇이 있는가** 는 :mod:`xgen_sdk.approval.catalog` 이 선언한다. 세 레포가
    같은 목록을 본다 — core 의 [결재 목록 설정] 화면이 workflow 의 행위를
    보여 줘야 하기 때문이다.
  * **승인되면 무엇을 하는가** 는 행위를 가진 서비스가 여기에 꽂는다. core 는
    workflow 파드 안의 함수를 부를 수 없으므로, 적용은 소유자가 한다.

    from xgen_sdk.approval.registry import register_action

    def _apply(payload, request):
        ...  # 실제로 그 일을 한다

    register_action("agent.deploy", _apply)

계약 두 가지
------------
1. **applier 는 멱등이어야 한다.** 적용이 실패해 사람이 다시 시도할 수 있고,
   두 번 돌아도 결과가 같아야 한다.
2. **applier 의 실패는 승인을 되돌리지 않는다.** 사람의 결재는 이미 일어난
   사실이다. 실패는 ``apply_error`` 로 남고 화면이 그 사실을 보여 준다 —
   승인을 취소하는 쪽이 훨씬 나쁜 거짓말이다.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from xgen_sdk.approval.catalog import CATALOG, GENERIC, TEST, ActionSpec, spec

logger = logging.getLogger("approval-registry")

#: action_type → {"apply": fn, "label": str, "spec": ActionSpec|None}
_ACTIONS: Dict[str, Dict[str, Any]] = {}


def register_action(
    action_type: str,
    apply: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
    *,
    reject: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
    label: str = "",
    action_spec: Optional[ActionSpec] = None,
) -> None:
    """결재 종류를 등록한다. ``apply`` 가 없으면 승인 자체가 결론이다.

    ``reject`` 는 **거절·회수된 뒤 치울 것이 있을 때** 쓴다. 지식 문서 업로드
    처럼 "일단 올려 두고 승인 뒤에 보이게" 하는 행위는, 거절되면 올려 둔 것을
    치워야 한다 — 안 치우면 거절된 문서가 아무도 모르는 채 남는다.

    **이미 등록된 것을 덮어쓰지 않는다** — 카탈로그가 먼저 이름표를 달아 두고,
    소유 서비스가 나중에 훅만 꽂는 순서이기 때문이다. 나중에 온 쪽이
    label 을 비워 보내면 앞서 달린 이름표가 그대로 남는다.
    """
    key = str(action_type or "").strip()
    if not key:
        raise ValueError("action_type 이 비어 있습니다")
    prev = _ACTIONS.get(key) or {}
    sp = action_spec or prev.get("spec") or spec(key)
    _ACTIONS[key] = {
        "apply": apply if apply is not None else prev.get("apply"),
        "reject": reject if reject is not None else prev.get("reject"),
        "label": label or (sp.label if sp else "") or prev.get("label") or key,
        "spec": sp,
    }


def is_registered(action_type: str) -> bool:
    return str(action_type or "") in _ACTIONS


def is_user_submittable(action_type: str) -> bool:
    """사람이 마이페이지에서 **직접** 올릴 수 있는 종류인가.

    게이트 행위(배포·컬렉션 생성·도구 게시…)는 아니다. payload 를 손으로 적어
    올릴 수 있으면 남의 워크플로우를 배포시키는 길이 된다 — 그 행위들은
    **기능 쪽 코드만** 올린다.
    """
    sp = (_ACTIONS.get(str(action_type or "")) or {}).get("spec")
    return bool(sp.user_submittable) if sp else True


def known_actions() -> Dict[str, str]:
    """등록된 종류 → 사람이 읽는 이름."""
    return {k: v["label"] for k, v in sorted(_ACTIONS.items())}


def submittable_actions() -> Dict[str, str]:
    """마이페이지 [결재 올리기] 의 선택지 — 자유 결재만."""
    return {k: v["label"] for k, v in sorted(_ACTIONS.items())
            if is_user_submittable(k)}


def has_apply(action_type: str) -> bool:
    """적용 훅이 이 프로세스에 꽂혀 있는가.

    없다고 등록되지 않은 것은 아니다 — 소유가 다른 서비스라 **이 파드에는**
    없는 것일 수 있다. 결정(core)과 적용(소유 서비스)이 다른 파드라서 생기는
    구분이고, ``store.decide`` 가 이것으로 "지금 적용할 것인가" 를 가른다.
    """
    return (_ACTIONS.get(str(action_type or "")) or {}).get("apply") is not None


def has_reject(action_type: str) -> bool:
    return (_ACTIONS.get(str(action_type or "")) or {}).get("reject") is not None


def owner_of(action_type: str) -> str:
    """이 행위의 적용을 책임지는 서비스. 카탈로그에 없으면 core 로 본다
    (테스트가 즉석에서 등록한 종류 — 등록한 그 자리에서 적용된다)."""
    sp = (_ACTIONS.get(str(action_type or "")) or {}).get("spec")
    return sp.owner if sp else "core"


def run_apply(action_type: str, payload: Dict[str, Any], request: Dict[str, Any]) -> str:
    """적용. 실패하면 **사유 문자열**을 돌려준다(예외를 던지지 않는다).

    던지면 호출부가 승인 기록까지 함께 잃을 위험이 있다. 이 함수의 일은
    "적용이 됐는가" 를 말하는 것이지 흐름을 끊는 것이 아니다.
    """
    entry = _ACTIONS.get(str(action_type or ""))
    if entry is None:
        # 등록이 사라진 종류(기능이 제거됐다). 승인은 그대로 두고 사실만 남긴다.
        return f"등록되지 않은 결재 종류입니다: {action_type}"
    fn = entry.get("apply")
    if fn is None:
        return ""
    try:
        fn(payload or {}, request or {})
        return ""
    except Exception as exc:  # noqa: BLE001 — 적용 실패가 승인을 무르지 않는다
        logger.exception("결재 적용 실패 (%s)", action_type)
        return f"{type(exc).__name__}: {exc}"[:1000]


def run_reject(action_type: str, payload: Dict[str, Any], request: Dict[str, Any]) -> str:
    """거절·회수 뒤 치우기. :func:`run_apply` 와 같은 계약 — 사유를 돌려준다."""
    entry = _ACTIONS.get(str(action_type or ""))
    if entry is None:
        return f"등록되지 않은 결재 종류입니다: {action_type}"
    fn = entry.get("reject")
    if fn is None:
        return ""
    try:
        fn(payload or {}, request or {})
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("결재 되돌리기 실패 (%s)", action_type)
        return f"{type(exc).__name__}: {exc}"[:1000]


def _apply_test(payload: Dict[str, Any], request: Dict[str, Any]) -> None:
    """테스트 결재의 적용 — **로그 한 줄이 전부다.**

    일부러 아무것도 바꾸지 않는다. 결재선이 도는지 보려고 올린 건이 시스템
    어딘가를 실제로 건드리면, 확인하려던 사람이 뒷정리를 해야 한다.
    """
    logger.info(
        "테스트 결재 #%s '%s' 최종 승인 — 적용 단계까지 도달(바꾸는 것 없음)",
        request.get("id"), request.get("title"),
    )


# 카탈로그가 먼저 이름표를 단다. 적용 훅은 소유 서비스가 나중에 꽂는다 —
# 그래서 어느 파드에서 물어도 **목록은 같다**.
for _spec in CATALOG:
    register_action(_spec.action_type, None, action_spec=_spec)

register_action(TEST, _apply_test)
