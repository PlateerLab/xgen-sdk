"""**무엇이 결재를 받아야 하는가** — 정책 원장.

원칙: 기본은 아무것도 막지 않는다
---------------------------------
표에 행이 없으면 그 행위는 결재가 필요 없다. 관리자가 [결재 목록 설정] 에서
켠 것만 결재를 탄다. 그래서 이 기능을 배포해도 **그날 아무도 막히지 않는다**.

읽기가 실패하면 어떻게 되나
---------------------------
예외를 그대로 올린다. 결재 필요 여부를 모르는 채로 "필요 없음" 으로 진행하면
통제를 켜 둔 조직에서 조용히 게이트가 열리고, "필요함" 으로 진행하면 DB 가
흔들릴 때 전 사용자가 작업을 못 한다. 둘 다 나쁘므로 **판단을 부르는 쪽**이
정하게 한다 — 호출부는 대개 "설정을 읽지 못했습니다" 로 400 을 내면 된다.

전역 스위치는 여기 없다
-----------------------
슈퍼유저 면제는 행위별 값이 아니라 전체에 걸리는 하나라서 설정 사다리
(``APPROVAL_SUPERUSER_EXEMPT``)에 산다. 이 모듈은 그 값을 **해석만** 한다
(:func:`superuser_exempt`) — 읽어 오는 것은 서비스마다 다른 composer 다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from xgen_sdk.approval import catalog
from xgen_sdk.approval.sql import q as _q

logger = logging.getLogger("approval-policy")

#: "안 줬다" 와 "비우라" 를 가르는 표식 — ``default_line_id=None`` 은 지우라는 뜻이다.
_SENTINEL = object()

#: 전역 스위치의 설정 키. core 가 ``create_persistent_config`` 로 선언하고,
#: 위성 서비스는 composer 로 읽는다.
SUPERUSER_EXEMPT_KEY = "APPROVAL_SUPERUSER_EXEMPT"

#: [결재 로그] 의 설정 변경 축에서 전역 스위치를 가리키는 이름.
SUPERUSER_EXEMPT_TARGET = "superuser_exempt"

_TRUE = ("1", "true", "yes", "on", "t", "y")


def superuser_exempt(value: Any) -> bool:
    """설정값 → 면제 여부. **기본은 면제**(True).

    지금까지 배포·RAG 통제 모두 superuser 의 행위는 게이트를 건너뛰었다.
    결재로 옮기면서 그 동작을 말없이 뒤집으면, 어제까지 되던 관리 작업이
    오늘 결재를 기다리게 된다. 바꾸고 싶으면 화면에서 끄면 된다.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


# ── 읽기 ──────────────────────────────────────────────────────────────


def get(app_db, action_type: str) -> Dict[str, Any]:
    """한 행위의 정책. 표에 없으면 **결재 불필요**가 답이다."""
    rows = _q(app_db,
              "SELECT action_type, required, default_line_id, line_locked, updated_by, updated_at "
              "FROM approval_action_policies WHERE action_type = %s",
              (str(action_type or ""),))
    if not rows:
        return {"action_type": action_type, "required": False,
                "default_line_id": None, "line_locked": False,
                "updated_by": None, "updated_at": None}
    row = dict(rows[0])
    row["required"] = bool(row.get("required"))
    # 고정은 결재선이 있을 때만 뜻이 있다 — 줄이 지워졌는데(FK SET NULL) 고정
    # 플래그만 남으면 "고정됐는데 아무것도 없는" 상태가 된다.
    row["line_locked"] = bool(row.get("line_locked")) and bool(row.get("default_line_id"))
    return row


def is_required(app_db, action_type: str) -> bool:
    """이 행위가 지금 결재를 받아야 하는가."""
    return bool(get(app_db, action_type)["required"])


def list_all(app_db) -> List[Dict[str, Any]]:
    """**카탈로그 전체** × 저장된 정책 — [결재 목록 설정] 화면의 한 장.

    저장된 것만 돌려주면 아직 켜 본 적 없는 행위가 화면에서 사라진다. 그러면
    관리자는 그 행위를 영영 켤 수 없다 — 목록에 없으니까.
    """
    saved = {r["action_type"]: r for r in _q(
        app_db,
        "SELECT p.action_type, p.required, p.default_line_id, p.line_locked, "
        "       p.updated_by, p.updated_at, "
        "       l.name AS default_line_name, "
        "       COALESCE(u.full_name, u.username) AS updated_by_name "
        "  FROM approval_action_policies p "
        "  LEFT JOIN approval_lines l ON l.id = p.default_line_id "
        "  LEFT JOIN users u ON u.id = p.updated_by",
    )}
    out: List[Dict[str, Any]] = []
    for sp in catalog.gated_specs():
        row = saved.get(sp.action_type) or {}
        out.append({
            **sp.to_dict(),
            "required": bool(row.get("required")),
            "default_line_id": row.get("default_line_id"),
            "line_locked": bool(row.get("line_locked")) and bool(row.get("default_line_id")),
            "default_line_name": row.get("default_line_name"),
            "updated_by": row.get("updated_by"),
            "updated_by_name": row.get("updated_by_name"),
            "updated_at": row.get("updated_at"),
        })
    return out


def required_actions(app_db) -> List[str]:
    """지금 결재를 타는 행위들 — 화면 요약과 위성 서비스의 빠른 판정용."""
    return [r["action_type"] for r in _q(
        app_db,
        "SELECT action_type FROM approval_action_policies WHERE required = TRUE")]


# ── 쓰기 ──────────────────────────────────────────────────────────────


def set_policy(app_db, action_type: str, *, actor_id: Optional[int],
               required: Optional[bool] = None,
               default_line_id: Any = _SENTINEL,
               line_locked: Optional[bool] = None) -> Dict[str, Any]:
    """정책 한 줄을 바꾼다. 바뀐 것만 이력에 남는다.

    ``default_line_id`` 는 ``None`` 을 **지우라는 뜻**으로 써야 해서 기본값을
    따로 뒀다 — 안 주면 그대로, ``None`` 을 주면 없앤다.

    ``line_locked`` 는 결재선이 있어야만 켤 수 있다 — 아무것도 없는 것을 고정할
    수는 없다. 결재선을 지우면 고정도 함께 풀린다.
    """
    key = str(action_type or "").strip()
    if not catalog.spec(key):
        raise ValueError(f"알 수 없는 행위입니다: {action_type}")
    sp = catalog.spec(key)
    if not sp.gated:
        raise ValueError(f"{sp.label} 은(는) 결재 목록으로 켜고 끄는 행위가 아닙니다")

    before = get(app_db, key)
    now = datetime.now(timezone.utc)
    new_required = before["required"] if required is None else bool(required)
    new_line = (before["default_line_id"] if default_line_id is _SENTINEL
                else (int(default_line_id) if default_line_id else None))

    # 결재선은 두 모드다.
    #   고정 안 함(기본): 여기 있는 줄은 요청 화면의 모달을 **미리 채우는** 편의일
    #                    뿐이고, 요청자가 바꿀 수 있다. 없어도 된다.
    #   고정:            요청자는 이 줄로만 올릴 수 있다. 관리자의 명시적 선택이다.
    new_locked = before["line_locked"] if line_locked is None else bool(line_locked)
    if not new_line:
        # 줄이 없으면 고정도 없다. 지금 **고정하라고** 온 요청이면 거절하고,
        # 줄을 지우러 온 요청이면 고정을 함께 푼다 — "고정됐는데 아무것도 없는"
        # 상태를 남기지 않는다.
        if line_locked is True:
            raise ValueError("결재선을 먼저 지정해야 고정할 수 있습니다")
        new_locked = False

    _q(app_db,
       """INSERT INTO approval_action_policies
              (action_type, required, default_line_id, line_locked, updated_by, updated_at)
          VALUES (%s, %s, %s, %s, %s, %s)
          ON CONFLICT (action_type) DO UPDATE
             SET required = EXCLUDED.required,
                 default_line_id = EXCLUDED.default_line_id,
                 line_locked = EXCLUDED.line_locked,
                 updated_by = EXCLUDED.updated_by,
                 updated_at = EXCLUDED.updated_at""",
       (key, new_required, new_line, new_locked, actor_id, now))

    if new_required != before["required"]:
        record_change(app_db, key,
                      "결재 필수" if before["required"] else "결재 불필요",
                      "결재 필수" if new_required else "결재 불필요",
                      actor_id)
    if new_line != before["default_line_id"]:
        record_change(app_db, f"{key}:default_line",
                      str(before["default_line_id"] or "없음"),
                      str(new_line or "없음"), actor_id)
    if new_locked != before["line_locked"]:
        record_change(app_db, f"{key}:line_locked",
                      "결재선 고정" if before["line_locked"] else "요청자가 선택",
                      "결재선 고정" if new_locked else "요청자가 선택",
                      actor_id)
    return get(app_db, key)


def record_change(app_db, target: str, before: Any, after: Any,
                  actor_id: Optional[int]) -> None:
    """설정 변경 한 줄. **실패해도 설정 변경을 무르지 않는다** — 이미 바뀐 뒤다."""
    try:
        _q(app_db,
           "INSERT INTO approval_policy_history (target, before, after, changed_by) "
           "VALUES (%s, %s, %s, %s)",
           (str(target)[:64], str(before)[:300] if before is not None else None,
            str(after)[:300] if after is not None else None, actor_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("결재 설정 이력 기록 실패 (%s): %s", target, exc)


def history(app_db, limit: int = 200) -> List[Dict[str, Any]]:
    return _q(app_db,
              "SELECT h.id, h.target, h.before, h.after, h.changed_by, h.created_at, "
              "       COALESCE(u.full_name, u.username) AS changed_by_name "
              "  FROM approval_policy_history h "
              "  LEFT JOIN users u ON u.id = h.changed_by "
              " ORDER BY h.id DESC LIMIT %s",
              (max(1, min(int(limit), 1000)),))


# ── 결재선 정하기 ─────────────────────────────────────────────────────


def resolve_line(app_db, action_type: str, *,
                 line_id: Optional[int] = None,
                 steps: Optional[List[Dict[str, Any]]] = None):
    """이 결재를 **누가** 처리하는가.

    두 모드가 있고, 관리자가 [결재 목록 설정] 에서 정한다.

    **결재선 고정** — 그 줄로만 올라간다. 요청이 다른 결재선을 들고 왔으면
    거절한다(:class:`ApprovalError`). 조용히 바꿔치기하면 화면은 "내가 고른
    결재자에게 갔다" 고 믿는데 실제로는 다른 사람에게 가 있다. 우리 화면은
    고정이면 고르는 자리 자체를 보여 주지 않으므로, 여기 걸리는 것은 낡은
    화면이거나 손으로 만든 요청이다 — 그건 소리를 내야 한다.

    **고정 안 함**(기본) — 요청자가 그 자리에서 고른 결재선이 이긴다. 관리자가
    정해 둔 기본 결재선은 모달을 **미리 채우는** 편의일 뿐이다. 둘 다 없으면
    :class:`ApprovalLineRequired` — 실패가 아니라 한 단계 덜 온 것이라, 화면은
    오류창 대신 결재선 모달을 띄우고 사용자가 고른 뒤 같은 요청을 다시 보낸다.

    반환: ``(line_id, steps)`` — 둘 중 하나만 채워져 있다.
    """
    from xgen_sdk.approval import catalog
    from xgen_sdk.approval.engine import ApprovalError, ApprovalLineRequired

    pol: Dict[str, Any] = {}
    try:
        pol = get(app_db, action_type)
    except Exception as exc:  # noqa: BLE001 — 못 읽으면 고정도 기본도 없는 것으로
        logger.warning("결재 정책 조회 실패 (%s): %s", action_type, exc)
    default_line = pol.get("default_line_id")

    if pol.get("line_locked") and default_line:
        if steps or (line_id and int(line_id) != int(default_line)):
            sp = catalog.spec(action_type)
            raise ApprovalError(
                f"{sp.label if sp else action_type} 은(는) 결재선이 고정된 행위입니다 — "
                "결재선을 고를 수 없습니다")
        return int(default_line), None

    if steps:
        return None, list(steps)
    if line_id:
        return int(line_id), None
    if default_line:
        return int(default_line), None

    sp = catalog.spec(action_type)
    raise ApprovalLineRequired(action_type, sp.label if sp else action_type, None)


def locked_lines(app_db) -> Dict[str, int]:
    """행위 → 고정된 결재선 id. 화면이 **고르는 자리를 아예 보여 주지 않기** 위해 본다."""
    return {r["action_type"]: int(r["default_line_id"]) for r in _q(
        app_db,
        "SELECT action_type, default_line_id FROM approval_action_policies "
        "WHERE line_locked = TRUE AND default_line_id IS NOT NULL")}


def required_for(app_db, action_types: Optional[List[str]] = None) -> Dict[str, bool]:
    """행위 → 지금 결재를 타는가. 화면이 **행동 전에** 물어보는 자리다.

    화면은 이 답으로 "버튼을 누르면 결재선 모달을 띄울지" 를 정한다. 서버도
    같은 판정을 하지만(그쪽이 최종), 화면이 미리 알아야 사용자가 요청을 보낸
    뒤에 거절당하는 대신 **처음부터 결재선을 고르고** 보낼 수 있다.
    """
    from xgen_sdk.approval import catalog

    wanted = list(action_types) if action_types else [s.action_type for s in catalog.gated_specs()]
    on = set(required_actions(app_db))
    return {a: (a in on) for a in wanted}
