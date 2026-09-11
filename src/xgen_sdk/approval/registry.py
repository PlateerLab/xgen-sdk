"""결재가 승인됐을 때 **무엇을 할지** 를 꽂는 자리.

왜 레지스트리인가
-----------------
"어떤 API 에 결재를 태울지" 는 아직 정해지지 않았고, 정해질 때마다 결재
엔진을 고치게 두면 안 된다. 그래서 엔진은 ``action_type`` 문자열만 알고,
그 문자열이 무엇을 뜻하는지는 **기능 쪽이 등록**한다.

    from xgen_sdk.approval.registry import register_action

    def _apply(payload, request):
        ...  # 실제로 그 일을 한다

    register_action("db.connection.activate", _apply,
                    label="DB 연결 활성화")

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

logger = logging.getLogger("approval-registry")

#: action_type → {"apply": fn, "label": str}
_ACTIONS: Dict[str, Dict[str, Any]] = {}

#: 아무 일도 하지 않는 기본 종류. **승인 자체가 결론**인 결재다.
#: 아직 어떤 API 도 결재를 타지 않으므로 지금은 전부 이것으로 올라온다.
GENERIC = "generic"


def register_action(
    action_type: str,
    apply: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
    *,
    label: str = "",
) -> None:
    """결재 종류를 등록한다. ``apply`` 가 없으면 승인 자체가 결론이다."""
    key = str(action_type or "").strip()
    if not key:
        raise ValueError("action_type 이 비어 있습니다")
    _ACTIONS[key] = {"apply": apply, "label": label or key}


def is_registered(action_type: str) -> bool:
    return str(action_type or "") in _ACTIONS


def known_actions() -> Dict[str, str]:
    """등록된 종류 → 사람이 읽는 이름. 화면의 선택지가 여기서 나온다."""
    return {k: v["label"] for k, v in sorted(_ACTIONS.items())}


def run_apply(action_type: str, payload: Dict[str, Any], request: Dict[str, Any]) -> str:
    """최종 승인 뒤 적용. 실패하면 **사유 문자열**을 돌려준다(예외를 던지지 않는다).

    던지면 호출부가 승인 기록까지 함께 잃을 위험이 있다. 이 함수의 일은
    "적용이 됐는가" 를 말하는 것이지 흐름을 끊는 것이 아니다.
    """
    spec = _ACTIONS.get(str(action_type or ""))
    if spec is None:
        # 등록이 사라진 종류(기능이 제거됐다). 승인은 그대로 두고 사실만 남긴다.
        return f"등록되지 않은 결재 종류입니다: {action_type}"
    fn = spec.get("apply")
    if fn is None:
        return ""
    try:
        fn(payload or {}, request or {})
        return ""
    except Exception as exc:  # noqa: BLE001 — 적용 실패가 승인을 무르지 않는다
        logger.exception("결재 적용 실패 (%s)", action_type)
        return f"{type(exc).__name__}: {exc}"[:1000]


#: 결재선을 **실제로 태워 보는** 종류. 아직 어떤 API 도 결재를 타지 않으므로,
#: 이것이 없으면 결재선이 도는지 확인할 방법이 없다 — 확인할 수 없는 체계는
#: 켜 두고도 아무도 믿지 않는다.
TEST = "test"


def _apply_test(payload: Dict[str, Any], request: Dict[str, Any]) -> None:
    """테스트 결재의 적용 — **로그 한 줄이 전부다.**

    일부러 아무것도 바꾸지 않는다. 결재선이 도는지 보려고 올린 건이 시스템
    어딘가를 실제로 건드리면, 확인하려던 사람이 뒷정리를 해야 한다.
    """
    logger.info(
        "테스트 결재 #%s '%s' 최종 승인 — 적용 단계까지 도달(바꾸는 것 없음)",
        request.get("id"), request.get("title"),
    )


# 기본 종류 — 무엇에 결재를 태울지 정해지기 전에도 결재 자체는 돈다.
register_action(GENERIC, None, label="일반 승인 (적용 동작 없음)")
register_action(TEST, _apply_test, label="테스트 결재 (결재선 확인용)")
