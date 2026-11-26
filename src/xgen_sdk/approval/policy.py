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

조건 규칙(2.30.0)
-----------------
행위마다 **순서 있는 규칙 목록**을 둘 수 있다. 규칙은 조건들(모두 맞아야 한다)과 결과 하나다.
조건은 요청이 실어 보낸 값(``metrics`` — 카탈로그가 행위마다 선언한 이름 → 숫자 · 문자열 · 불리언)에 대한
비교이고, 결과는 셋 중 하나다:

  * ``auto_approve`` — 사람 없이 승인한다. 결재 건은 「승인(규칙 #n 으로 자동 승인)」 으로 원장에 남고
    적용 훅은 사람이 승인했을 때와 같은 길로 돈다(:func:`xgen_sdk.approval.store.submit`).
  * ``line``         — 그 결재선으로만 올라간다(고정 결재선과 같다 — 요청자가 바꿀 수 없다).
  * ``require``      — 결재를 받아야 한다(행위가 꺼져 있어도). 결재선은 지금 정책대로 고른다.

**처음 맞는 규칙이 이긴다.** 맞는 규칙이 없거나, 요청이 값을 보내지 않았으면(``metrics=None``) 지금
정책 그대로다 — 규칙이 없는 설치와 ``metrics`` 를 모르는 호출부는 2.29.0 과 똑같이 돈다.
값이 빠진 조건은 맞지 않는 것으로 본다(값을 안 보낸 요청이 자동 승인으로 새지 않게).

규칙은 ``approval_action_policies.rules`` (JSON)에 둔다. 칸이 아직 없는 DB(core 가 새 SDK 로 표를 고치기
전)에서는 읽기가 실패하고 **규칙이 없는 것**으로 본다.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

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


def is_required(app_db, action_type: str,
                metrics: Optional[Mapping[str, Any]] = None) -> bool:
    """이 행위가 지금 결재를 받아야 하는가 — 정확히는 **결재 원장을 거쳐야 하는가**.

    ``metrics`` 를 주면 조건 규칙까지 본다. 규칙이 맞으면 결과와 상관없이 참이다 — 자동 승인(``auto_approve``)도
    원장에 「자동 승인」 으로 남아야 하므로 호출부는 평소처럼 :func:`resolve_line` → ``store.submit`` 으로
    올리고, 거기서 사람 없이 승인된다. 맞는 규칙이 없거나 ``metrics`` 를 안 주면 지금과 같다.
    """
    if metrics is not None and evaluate(app_db, action_type, metrics):
        return True
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
    rules = _all_rules(app_db)
    out: List[Dict[str, Any]] = []
    for sp in catalog.gated_specs():
        row = saved.get(sp.action_type) or {}
        out.append({
            "rules": rules.get(sp.action_type, []),
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
    """지금 결재를 타는 행위들 — 화면 요약과 위성 서비스의 빠른 판정용.

    카탈로그에 있는 것만 — 거둔 행위(:data:`catalog.RETIRED`)의 정책 행이 아직 치워지기
    전이라도 화면이 그 종류를 "결재 필수" 로 알아서는 안 된다.
    """
    return [r["action_type"] for r in _q(
        app_db,
        "SELECT action_type FROM approval_action_policies WHERE required = TRUE")
        if catalog.spec(r["action_type"])]


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
        raise ValueError(f"{sp.label}은(는) 결재 필수로 지정할 수 없는 행위입니다.")

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
                 steps: Optional[List[Dict[str, Any]]] = None,
                 metrics: Optional[Mapping[str, Any]] = None):
    """이 결재를 **누가** 처리하는가.

    ``metrics`` (2.30.0) — 요청이 실어 보낸 값. 주면 조건 규칙을 먼저 본다(:func:`evaluate`):

      * ``auto_approve`` 가 맞으면 ``(None, None)`` — 사람이 없다. 같은 ``metrics`` 로 ``store.submit`` 을
        부르면 자동 승인된다. (규칙이 없을 때 이 함수는 ``(None, None)`` 을 돌려주는 일이 없다.)
      * ``line`` 이 맞으면 그 결재선이 **고정 결재선**처럼 쓰인다 — 다른 줄을 들고 오면 거절한다.
      * ``require`` 이거나 맞는 규칙이 없으면 아래의 지금 규칙 그대로다.

    ``metrics`` 를 안 주면 규칙을 읽지도 않는다 — 2.29.0 과 같다.

    두 모드가 있고, 관리자가 [결재 목록 설정] 에서 정한다.

    **결재선 고정** — 그 줄로만 올라간다. 요청이 다른 결재선을 들고 왔으면
    거절한다(:class:`ApprovalError`). 조용히 바꿔치기하면 화면은 "내가 고른
    결재자에게 갔다" 고 믿는데 실제로는 다른 사람에게 가 있다. 우리 화면은
    고정이면 고르는 자리 자체를 보여 주지 않으므로, 여기 걸리는 것은 낡은
    화면이거나 손으로 만든 요청이다 — 그건 소리를 내야 한다.

    단, 고정된 줄에 **임의 차례**(사람이 비어 있는 차례, 1.62.0)가 있으면 요청자가 그
    차례들의 사람을 골라 steps 로 들고 와야 한다. 사람이 정해진 차례는 바꿀 수 없고
    차례 수도 그대로다(:func:`engine.fill_open_slots`). 안 들고 왔으면
    :class:`ApprovalLineRequired` — 모달이 그 줄로 열려 임의 차례를 채우게 한다.
    고정 안 한 줄도 줄만 들고 왔는데 임의 차례가 있으면 같은 이유로 되돌린다.

    **고정 안 함**(기본) — 요청자가 그 자리에서 고른 결재선이 이긴다. 관리자가
    정해 둔 기본 결재선은 모달을 **미리 채우는** 편의일 뿐이다. 둘 다 없으면
    :class:`ApprovalLineRequired` — 실패가 아니라 한 단계 덜 온 것이라, 화면은
    오류창 대신 결재선 모달을 띄우고 사용자가 고른 뒤 같은 요청을 다시 보낸다.

    반환: ``(line_id, steps)``.

    **둘 다 채워질 수 있다** — 예전에는 "둘 중 하나" 였는데, 결재선이 **양식**을
    지니게 되면서 그 규칙이 기능을 망가뜨렸다: 요청자가 결재자를 한 명이라도
    손대면 ``(None, steps)`` 가 되어 **결재선이 사라지고, 그와 함께 양식도
    사라졌다.** 관리자가 "이 행위는 이 파이프라인(결재선+양식)으로" 라고 정해
    뒀는데 배포 요청에는 기안 칸이 하나도 붙지 않는, 정확히 그 사고다
    (2026-09-13 실측).

    그래서 **누가 처리하는가**(steps)와 **무슨 절차인가**(line_id)를 갈라 둔다:
    요청자가 고른 사람이 결재자가 되고, 그 사람들이 어느 줄에서 왔는지는
    양식을 찾는 데 쓴다. 결재자 수가 양식을 못 덮으면 :func:`store.submit` 이
    거절한다.

    줄에서 오지 않은 결재선(사용자가 처음부터 손으로 고른 것)은 line_id 가
    ``None`` 이고, 그때는 양식도 없다 — 그것이 맞다.
    """
    from xgen_sdk.approval import catalog
    from xgen_sdk.approval import engine as E
    from xgen_sdk.approval.engine import ApprovalError, ApprovalLineRequired

    pol: Dict[str, Any] = {}
    try:
        pol = get(app_db, action_type)
    except Exception as exc:  # noqa: BLE001 — 못 읽으면 고정도 기본도 없는 것으로
        logger.warning("결재 정책 조회 실패 (%s): %s", action_type, exc)
    default_line = pol.get("default_line_id")

    sp = catalog.spec(action_type)
    label = sp.label if sp else action_type

    if metrics is not None:
        hit = evaluate(app_db, action_type, metrics)
        if hit and hit["outcome"] == OUTCOME_AUTO:
            return None, None
        if hit and hit["outcome"] == OUTCOME_LINE:
            ruled = int(hit["line_id"])
            template = _line_slots(app_db, ruled)
            if not template:
                # 규칙을 저장한 뒤 결재선이 비었다 — 더 가벼운 줄로 조용히 넘기지 않는다.
                raise ApprovalError(
                    f"{label}의 조건 규칙 #{hit['rule']}이(가) 가리키는 결재선을 찾을 수 없습니다. "
                    "관리자에게 [결재 목록 설정]을 확인해 달라고 요청하세요.")
            return _locked_line(
                action_type, label, ruled, template, line_id, steps,
                ApprovalError(f"{label}은(는) 조건 규칙 #{hit['rule']}으로 결재선이 정해져 있어 바꿀 수 없습니다."))

    if pol.get("line_locked") and default_line:
        return _locked_line(
            action_type, label, int(default_line), _line_slots(app_db, int(default_line)),
            line_id, steps,
            ApprovalError(f"{label}은(는) 결재선이 고정되어 있어 바꿀 수 없습니다."))

    if steps:
        # 요청이 어느 줄에서 왔는지 말해 주면 그것을, 아니면 이 행위의 기본
        # 결재선을 **양식의 출처**로 삼는다. 결재자는 어디까지나 steps 다.
        origin = line_id if line_id else default_line
        return (int(origin) if origin else None), list(steps)
    for candidate in (line_id, default_line):
        if not candidate:
            continue
        # 줄만 들고 왔는데 그 줄에 **임의 차례**가 있으면 그대로는 못 올린다 — 빈 차례로
        # 올라가면 거기서 멈춘다. 모달을 그 줄로 미리 채워 사람을 고르게 한다.
        if any(E.is_open_slot(s) for s in _line_slots(app_db, int(candidate))):
            raise ApprovalLineRequired(action_type, label, int(candidate))
        return int(candidate), None

    raise ApprovalLineRequired(action_type, label, None)


def _locked_line(action_type: str, label: str, line: int, template: List[Dict[str, Any]],
                 line_id: Optional[int], steps: Optional[List[Dict[str, Any]]],
                 refused: Exception):
    """그 줄로만 올라가는 결재 — 관리자가 고정한 줄, 또는 조건 규칙이 고른 줄."""
    from xgen_sdk.approval import engine as E
    from xgen_sdk.approval.engine import ApprovalLineRequired

    if line_id and int(line_id) != int(line):
        raise refused
    if not any(E.is_open_slot(s) for s in template):
        # 사람이 모두 정해진 고정 결재선 — 그 줄로만 간다.
        if steps:
            raise refused
        return int(line), None
    # **임의 차례가 있는 고정 결재선**(1.62.0) — 요청자는 임의 차례의 사람만 고른다.
    # 아무것도 안 들고 왔으면 실패가 아니라 한 단계 덜 온 것이다: 화면은 모달을
    # 띄워 그 차례들을 채우게 하고 같은 요청을 다시 보낸다.
    if not steps:
        raise ApprovalLineRequired(action_type, label, int(line))
    return int(line), E.fill_open_slots(template, steps)


def _line_slots(app_db, line_id: int) -> List[Dict[str, Any]]:
    """결재선의 차례들 — 임의 차례는 ``approver_id`` 가 비어 있다."""
    return _q(app_db,
              "SELECT step_order, approver_id FROM approval_line_steps WHERE line_id = %s",
              (int(line_id),))


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


# ── 조건 규칙(2.30.0) ─────────────────────────────────────────────────

#: 이 SDK 가 조건 규칙을 아는가 — 호출부는 ``getattr(policy, "RULES_SUPPORTED", False)`` 로 본다.
RULES_SUPPORTED = True

OUTCOME_AUTO = "auto_approve"
OUTCOME_LINE = "line"
OUTCOME_REQUIRE = "require"
RULE_OUTCOMES = (OUTCOME_AUTO, OUTCOME_LINE, OUTCOME_REQUIRE)

#: 맞는 규칙이 없을 때 :func:`preview` 가 내는 결과 — 지금 정책(켜짐 · 기본 결재선 · 고정)대로.
OUTCOME_POLICY = "policy"

RULE_OPS = ("<=", ">=", "==", "!=", "in")
#: 종류마다 쓸 수 있는 비교. 크기 비교는 숫자에만 뜻이 있다.
_OPS_BY_KIND = {
    catalog.METRIC_NUMBER: RULE_OPS,
    catalog.METRIC_STRING: ("==", "!=", "in"),
    catalog.METRIC_BOOL: ("==", "!="),
}
MAX_RULES = 20
MAX_CONDITIONS = 10
MAX_IN_VALUES = 50
_LABEL_MAX = 100
_STRING_MAX = 200


def _is_number(value: Any) -> bool:
    # bool 은 int 의 하위형이지만 숫자로 받지 않는다 — True <= 2 가 맞는 것은 사고다.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _fits(kind: str, value: Any) -> bool:
    if kind == catalog.METRIC_NUMBER:
        return _is_number(value)
    if kind == catalog.METRIC_STRING:
        return isinstance(value, str)
    if kind == catalog.METRIC_BOOL:
        return isinstance(value, bool)
    return False


def validate_rules(action_type: str, rules: Any, *, app_db=None) -> List[Dict[str, Any]]:
    """규칙 목록 → 정돈한 목록. 틀리면 :class:`ValueError` (화면에 그대로 보일 문장).

    거절하는 것: 모르는 행위 · 값을 선언하지 않은 행위, 모르는 값 이름, 종류에 맞지 않는 비교나 값,
    모르는 결과, 결재선 없는 ``line``, (``app_db`` 를 주면) 없거나 내려간 결재선.
    """
    key = str(action_type or "").strip()
    sp = catalog.spec(key)
    if not sp:
        raise ValueError(f"알 수 없는 행위입니다: {action_type}")
    if rules is None:
        rules = []
    if not isinstance(rules, (list, tuple)):
        raise ValueError("조건 규칙은 목록이어야 합니다")
    if rules and not sp.gated:
        raise ValueError(f"{sp.label}에는 조건 규칙을 둘 수 없습니다.")
    if rules and not sp.metrics:
        raise ValueError(f"{sp.label}은(는) 요청에 실어 보내는 값이 없어 조건 규칙을 둘 수 없습니다.")
    if len(rules) > MAX_RULES:
        raise ValueError(f"조건 규칙은 {MAX_RULES}개까지 둘 수 있습니다")

    out: List[Dict[str, Any]] = []
    for i, raw in enumerate(rules, start=1):
        where = f"규칙 #{i}"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{where}: 형식이 올바르지 않습니다")
        conds = raw.get("conditions")
        if not isinstance(conds, (list, tuple)) or not conds:
            raise ValueError(f"{where}: 조건을 하나 이상 넣어 주세요")
        if len(conds) > MAX_CONDITIONS:
            raise ValueError(f"{where}: 조건은 {MAX_CONDITIONS}개까지 넣을 수 있습니다")
        clean_conds: List[Dict[str, Any]] = []
        for c in conds:
            if not isinstance(c, Mapping):
                raise ValueError(f"{where}: 조건 형식이 올바르지 않습니다")
            name = str(c.get("metric") or "").strip()
            m = sp.metric(name)
            if not m:
                raise ValueError(f"{where}: {sp.label}이(가) 보내지 않는 값입니다: {name or '(없음)'}")
            op = str(c.get("op") or "").strip()
            if op not in RULE_OPS:
                raise ValueError(f"{where}: 알 수 없는 비교입니다: {op or '(없음)'}")
            if op not in _OPS_BY_KIND.get(m.kind, ()):
                raise ValueError(f"{where}: {m.label}에는 {op} 비교를 쓸 수 없습니다")
            value = c.get("value")
            if op == "in":
                if not isinstance(value, (list, tuple)) or not value:
                    raise ValueError(f"{where}: {m.label}의 in 비교에는 값 목록이 필요합니다")
                if len(value) > MAX_IN_VALUES:
                    raise ValueError(f"{where}: in 비교의 값은 {MAX_IN_VALUES}개까지입니다")
                if not all(_fits(m.kind, v) for v in value):
                    raise ValueError(f"{where}: {m.label}의 값 종류가 맞지 않습니다")
                value = [v[:_STRING_MAX] if isinstance(v, str) else v for v in value]
            else:
                if not _fits(m.kind, value):
                    raise ValueError(f"{where}: {m.label}의 값 종류가 맞지 않습니다")
                if isinstance(value, str):
                    value = value[:_STRING_MAX]
            clean_conds.append({"metric": name, "op": op, "value": value})

        outcome = str(raw.get("outcome") or "").strip()
        if outcome not in RULE_OUTCOMES:
            raise ValueError(f"{where}: 알 수 없는 결과입니다: {outcome or '(없음)'}")
        line = None
        if outcome == OUTCOME_LINE:
            try:
                line = int(raw.get("line_id") or 0)
            except (TypeError, ValueError):
                line = 0
            if line <= 0:
                raise ValueError(f"{where}: 쓸 결재선을 골라 주세요")
            if app_db is not None and not _line_usable(app_db, line):
                raise ValueError(f"{where}: 결재선을 찾을 수 없습니다(#{line})")
        rule: Dict[str, Any] = {"conditions": clean_conds, "outcome": outcome, "line_id": line}
        label = str(raw.get("label") or "").strip()[:_LABEL_MAX]
        if label:
            rule["label"] = label
        out.append(rule)
    return out


def _line_usable(app_db, line_id: int) -> bool:
    rows = _q(app_db, "SELECT id, is_active FROM approval_lines WHERE id = %s", (int(line_id),))
    return bool(rows) and bool(rows[0].get("is_active", True))


def _decode_rules(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return raw
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def get_rules(app_db, action_type: str) -> List[Dict[str, Any]]:
    """한 행위의 조건 규칙(저장된 순서). 없거나, 칸이 아직 없는 DB 면 빈 목록 — 지금 정책대로 돈다."""
    try:
        rows = _q(app_db,
                  "SELECT rules FROM approval_action_policies WHERE action_type = %s",
                  (str(action_type or ""),))
    except Exception as exc:  # noqa: BLE001 — 규칙을 모르는 표 = 규칙이 없다
        logger.warning("결재 조건 규칙 조회 실패 — 규칙 없이 진행 (%s): %s", action_type, exc)
        return []
    return _decode_rules(rows[0].get("rules")) if rows else []


def _all_rules(app_db) -> Dict[str, List[Dict[str, Any]]]:
    try:
        rows = _q(app_db, "SELECT action_type, rules FROM approval_action_policies")
    except Exception as exc:  # noqa: BLE001
        logger.warning("결재 조건 규칙 조회 실패 — 규칙 없이 보인다: %s", exc)
        return {}
    return {str(r["action_type"]): _decode_rules(r.get("rules")) for r in rows}


def set_rules(app_db, action_type: str, rules: Any, *,
              actor_id: Optional[int]) -> List[Dict[str, Any]]:
    """한 행위의 조건 규칙을 **통째로** 바꾼다(순서가 곧 우선순위). 빈 목록이면 규칙을 모두 지운다.

    검증은 :func:`validate_rules` 가 한다(결재선이 있는지까지). 켜짐 · 기본 결재선 · 고정은 건드리지 않는다.
    바뀌었으면 설정 이력(``{action}:rules``)에 앞뒤 규칙 수를 남긴다.
    """
    key = str(action_type or "").strip()
    clean = validate_rules(key, rules, app_db=app_db)
    before_rules = get_rules(app_db, key)
    before = get(app_db, key)
    now = datetime.now(timezone.utc)
    _q(app_db,
       """INSERT INTO approval_action_policies
              (action_type, required, default_line_id, line_locked, rules, updated_by, updated_at)
          VALUES (%s, %s, %s, %s, %s, %s, %s)
          ON CONFLICT (action_type) DO UPDATE
             SET rules = EXCLUDED.rules,
                 updated_by = EXCLUDED.updated_by,
                 updated_at = EXCLUDED.updated_at""",
       (key, bool(before["required"]), before["default_line_id"], bool(before["line_locked"]),
        json.dumps(clean, ensure_ascii=False), actor_id, now))
    if clean != before_rules:
        record_change(app_db, f"{key}:rules",
                      describe_rules(key, before_rules), describe_rules(key, clean), actor_id)
    return get_rules(app_db, key)


def describe_rules(action_type: str, rules: Sequence[Mapping[str, Any]]) -> str:
    """규칙 목록 → 한 줄 문장(설정 이력 · 감사용). ``규칙 없음`` 또는 ``#1 메모리 <= 2048 → 자동 승인; ...``."""
    if not rules:
        return "규칙 없음"
    return "; ".join(f"#{i} {describe_rule(action_type, r)}" for i, r in enumerate(rules, start=1))


_OUTCOME_TEXT = {OUTCOME_AUTO: "자동 승인", OUTCOME_LINE: "결재선", OUTCOME_REQUIRE: "결재 필요"}


def describe_rule(action_type: str, rule: Mapping[str, Any]) -> str:
    sp = catalog.spec(action_type)
    parts = []
    for c in rule.get("conditions") or []:
        m = sp.metric(str(c.get("metric"))) if sp else None
        name = m.label if m else str(c.get("metric"))
        value = c.get("value")
        if isinstance(value, bool):
            value = "예" if value else "아니오"
        elif isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        unit = f" {m.unit}" if m and m.unit and m.kind == catalog.METRIC_NUMBER else ""
        parts.append(f"{name} {c.get('op')} {value}{unit}")
    out = _OUTCOME_TEXT.get(str(rule.get("outcome")), str(rule.get("outcome")))
    if rule.get("outcome") == OUTCOME_LINE:
        out = f"결재선 #{rule.get('line_id')}"
    return f"{' 그리고 '.join(parts)} → {out}"


def _condition_holds(cond: Mapping[str, Any], metrics: Mapping[str, Any]) -> bool:
    name = cond.get("metric")
    if name not in metrics:
        return False                     # 값이 없으면 맞지 않는다
    actual = metrics.get(name)
    if actual is None:
        return False
    op = cond.get("op")
    want = cond.get("value")
    try:
        if op == "in":
            return isinstance(want, list) and any(_same(actual, v) for v in want)
        if op == "==":
            return _same(actual, want)
        if op == "!=":
            # 종류가 다른 값은 "다르다" 가 아니라 비교할 수 없는 것이다 — 맞지 않는 것으로 본다.
            return _comparable(actual, want) and not _same(actual, want)
        if op in ("<=", ">="):
            if not (_is_number(actual) and _is_number(want)):
                return False
            return float(actual) <= float(want) if op == "<=" else float(actual) >= float(want)
    except Exception:  # noqa: BLE001 — 이상한 값은 맞지 않는 것으로
        return False
    return False


def _comparable(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if _is_number(a) or _is_number(b):
        return _is_number(a) and _is_number(b)
    return isinstance(a, str) and isinstance(b, str)


def _same(a: Any, b: Any) -> bool:
    if not _comparable(a, b):
        return False
    if _is_number(a):
        return float(a) == float(b)
    return a == b


def match_rule(rules: Sequence[Mapping[str, Any]],
               metrics: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """규칙 목록에서 **처음 맞는 것** — DB 를 모르는 순수 함수.

    돌려주는 것: ``{"rule": 1부터 센 번호, "outcome", "line_id", "label", "conditions"}`` 또는 ``None``.
    ``metrics`` 가 없으면(``None`` · 사전이 아님) 아무것도 맞지 않는다.
    """
    if not isinstance(metrics, Mapping):
        return None
    for i, rule in enumerate(rules or [], start=1):
        conds = rule.get("conditions") or []
        if not conds or rule.get("outcome") not in RULE_OUTCOMES:
            continue
        if rule.get("outcome") == OUTCOME_LINE and not rule.get("line_id"):
            continue
        if all(_condition_holds(c, metrics) for c in conds):
            return {"rule": i, "outcome": rule["outcome"], "line_id": rule.get("line_id"),
                    "label": rule.get("label") or "", "conditions": list(conds)}
    return None


def evaluate(app_db, action_type: str,
             metrics: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """저장된 규칙으로 :func:`match_rule`. ``metrics`` 가 없으면 규칙을 읽지도 않는다."""
    if not isinstance(metrics, Mapping):
        return None
    rules = get_rules(app_db, action_type)
    return match_rule(rules, metrics) if rules else None


def clean_metrics(action_type: str, metrics: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """원장에 남길 값 — 카탈로그가 선언한 이름의 단순 값만(문자열은 잘라서)."""
    sp = catalog.spec(action_type)
    if not sp or not isinstance(metrics, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for m in sp.metrics:
        if m.name not in metrics:
            continue
        v = metrics[m.name]
        if isinstance(v, str):
            out[m.name] = v[:_STRING_MAX]
        elif isinstance(v, bool) or _is_number(v):
            out[m.name] = v
    return out


def preview(app_db, action_type: str,
            metrics: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """「이 값으로 요청하면 어떻게 되는가」 — 요청 화면의 안내와 설정 화면의 미리 보기용. 아무것도 쓰지 않는다.

    돌려주는 것::

        {"action_type", "rules_supported": True,
         "outcome": "auto_approve" | "line" | "require" | "policy",   # policy = 맞는 규칙 없음, 지금 정책대로
         "auto_approve": bool,      # 사람 없이 승인된다
         "required": bool,          # 사람의 결재를 받는다
         "rule": int | None, "rule_label": str,
         "line_id": int | None,     # 쓰일(또는 미리 채워질) 결재선
         "line_locked": bool}       # 요청자가 결재선을 바꿀 수 없다

    슈퍼유저 면제는 보지 않는다(요청자를 모른다) — 호출부가 면제면 결재 없이 진행하는 것은 지금과 같다.
    """
    pol = get(app_db, action_type)
    hit = evaluate(app_db, action_type, metrics)
    base = {"action_type": action_type, "rules_supported": True,
            "rule": hit["rule"] if hit else None,
            "rule_label": hit["label"] if hit else ""}
    if hit and hit["outcome"] == OUTCOME_AUTO:
        return {**base, "outcome": OUTCOME_AUTO, "auto_approve": True, "required": False,
                "line_id": None, "line_locked": False}
    if hit and hit["outcome"] == OUTCOME_LINE:
        return {**base, "outcome": OUTCOME_LINE, "auto_approve": False, "required": True,
                "line_id": int(hit["line_id"]), "line_locked": True}
    out = {**base, "auto_approve": False,
           "line_id": pol.get("default_line_id"), "line_locked": bool(pol.get("line_locked"))}
    if hit:
        return {**out, "outcome": OUTCOME_REQUIRE, "required": True}
    return {**out, "outcome": OUTCOME_POLICY, "required": bool(pol.get("required"))}


def rules_using_line(app_db, line_id: int) -> List[str]:
    """이 결재선을 **조건 규칙의 결과로** 쓰는 행위들 — 결재선을 내리기 전에 본다."""
    out = []
    for action, rules in _all_rules(app_db).items():
        if any(r.get("outcome") == OUTCOME_LINE and int(r.get("line_id") or 0) == int(line_id)
               for r in rules):
            out.append(action)
    return out


def release_line_from_rules(app_db, line_id: int, actor_id: Optional[int]) -> List[str]:
    """결재선을 내릴 때 그 줄을 쓰던 규칙을 ``require``(결재 필요)로 바꾼다 — 더 가벼운 쪽으로 새지 않게.

    결재선이 사라졌다고 규칙을 지우면 "8 GiB 넘으면 결재" 같은 조건이 함께 사라져 자동 승인 규칙이 대신 맞을 수 있다.
    """
    touched = []
    for action, rules in _all_rules(app_db).items():
        if not any(r.get("outcome") == OUTCOME_LINE and int(r.get("line_id") or 0) == int(line_id)
                   for r in rules):
            continue
        new = [dict(r, outcome=OUTCOME_REQUIRE, line_id=None)
               if r.get("outcome") == OUTCOME_LINE and int(r.get("line_id") or 0) == int(line_id)
               else r for r in rules]
        _q(app_db, "UPDATE approval_action_policies SET rules = %s WHERE action_type = %s",
           (json.dumps(new, ensure_ascii=False), action))
        record_change(app_db, f"{action}:rules", describe_rules(action, rules),
                      describe_rules(action, new), actor_id)
        touched.append(action)
    return touched
